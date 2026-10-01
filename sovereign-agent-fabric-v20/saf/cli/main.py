"""SAF CLI: doctor, capabilities, resources, run, execute, worker, sync, verify, ledger, rollback."""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shlex
import sys

from saf.core.compiler import compile_intent
from saf.core.contracts import ExecutionContext
from saf.core.policy import PolicyEngine
from saf.runtime.bootstrap import build_executor, build_resolver


def _print(payload: dict, *, as_json: bool = True) -> None:
    print(json.dumps(payload, indent=2, default=str) if as_json else payload)


def _local_run(intent: str, es: str | None, token: str | None, namespace: str,
               state_dir: str, queue_offline: bool) -> dict:
    task = compile_intent(intent)
    ctx = ExecutionContext(task_id="local-cli", platform=platform.system())
    allowed, reason = PolicyEngine().authorize(task)
    if not allowed:
        return {"status": "blocked", "intent": task.intent, "reason": reason,
                "capabilities": task.required_capabilities}
    ranked = build_resolver().resolve(task, ctx)
    out = {
        "status": "ok",
        "intent": task.intent,
        "capabilities": task.required_capabilities,
        "ranked_candidates": [x.model_dump() for x in ranked[:5]],
    }
    if not es:
        return out

    from saf.transport.effective_scale import EffectiveScaleTransport, TransportError
    from saf.transport.outbox import Outbox, reconcile

    transport = EffectiveScaleTransport(es, token=token, namespace=namespace)
    outbox = Outbox(f"{state_dir}/outbox.jsonl")
    try:
        submitted = asyncio.run(transport.submit(task))
        out["execution_plan"] = {
            "workflow_id": submitted["workflow"]["id"],
            "status": submitted["workflow"]["status"],
            "idempotency_key": submitted["idempotency_key"],
            "replayed": submitted["replayed"],
        }
    except TransportError as exc:
        if not queue_offline:
            out["execution_plan"] = {"status": "unavailable", "error": str(exc), "code": exc.code,
                                     "hint": "kernel unreachable — re-run without --no-queue to queue"}
            return out
        entry = outbox.enqueue("workflow.submit", {"intent": task.intent, "namespace": namespace})
        out["execution_plan"] = {
            "status": "queued_offline",
            "outbox_id": entry["id"],
            "payload_hash": entry["payload_hash"],
            "error": str(exc),
            "hint": "run `saf sync --es <kernel>` when the connection is back",
        }
        # opportunistic reconciliation: a transient outage should self-heal
        summary = asyncio.run(reconcile(outbox, transport, limit=5))
        out["execution_plan"]["sync"] = {"acked": len(summary["acked"]),
                                         "deferred": len(summary["deferred"])}
        if summary["acked"]:
            synced = summary["acked"][-1]
            out["execution_plan"].update({"status": "queued_and_synced",
                                          "workflow_id": synced["workflow_id"],
                                          "replayed": synced["replayed"]})
    return out


def _test_command(raw: str | None) -> list[str] | None:
    """Shell-quoted argv -> list. Never passed to a shell, so no injection."""
    return shlex.split(raw) if raw else None


def _execute(intent: str, args) -> int:
    executor = build_executor(args.workspace, state_dir=args.state_dir,
                              test_command=_test_command(args.test_command),
                              platform=platform.system())
    result = asyncio.run(executor.execute(compile_intent(intent)))
    _print(result.model_dump())
    return 0 if result.ok else 2


def _worker(args) -> int:
    from saf.core.ids import new_worker_id
    from saf.runtime.worker import KernelWorker
    from saf.transport.effective_scale import EffectiveScaleTransport, TransportError

    executor = build_executor(args.workspace, state_dir=args.state_dir,
                              test_command=_test_command(args.test_command),
                              platform=platform.system())
    transport = EffectiveScaleTransport(args.es, token=args.token, namespace=args.namespace)
    worker = KernelWorker(transport, executor, worker_id=args.worker_id or new_worker_id(),
                          claim_ttl=args.claim_ttl, poll_interval=args.poll_interval)
    try:
        summary = asyncio.run(worker.serve(
            max_jobs=args.max_jobs,
            idle_limit=1 if args.once else args.idle_limit))
    except TransportError as exc:
        _print({"status": "kernel_unavailable", "error": str(exc), "code": exc.code})
        return 1
    _print(summary)
    return 0 if summary.get("status") == "ok" else 1


def _sync(args) -> int:
    from saf.transport.effective_scale import EffectiveScaleTransport, TransportError
    from saf.transport.outbox import Outbox, reconcile

    outbox = Outbox(f"{args.state_dir}/outbox.jsonl")
    transport = EffectiveScaleTransport(args.es, token=args.token, namespace=args.namespace)
    try:
        summary = asyncio.run(reconcile(outbox, transport, limit=args.limit))
    except TransportError as exc:
        _print({"status": "kernel_unavailable", "error": str(exc), "outbox": outbox.summary()})
        return 1
    _print({"status": "ok", **summary, "outbox": outbox.summary()})
    return 0


def _verify(args) -> int:
    from saf.evidence.ledger import EvidenceLedger

    ledger = EvidenceLedger(f"{args.state_dir}/evidence.jsonl")
    report = ledger.verify()
    report["path"] = ledger.path
    _print(report)
    return 0 if report["ok"] else 1


def _ledger(args) -> int:
    from saf.evidence.ledger import EvidenceLedger

    records = EvidenceLedger(f"{args.state_dir}/evidence.jsonl").all()
    _print({"count": len(records), "records": records[-args.limit:]})
    return 0


def _rollback(args) -> int:
    from saf.tools.backup import BackupStore

    store = BackupStore(f"{args.state_dir}/backups")
    if args.execution_id in ("", "latest"):
        points = store.points()
        if not points:
            _print({"ok": False, "error": "no rollback points"})
            return 1
        args.execution_id = points[-1]["execution_id"]
    if args.dry_run:
        plan = store.plan(args.execution_id, workspace=args.workspace)
        _print({"execution_id": args.execution_id, "dry_run": True, "plan": plan})
        return 0
    result = store.restore(args.execution_id, workspace=args.workspace)
    _print(result)
    return 0 if result.get("ok") else 1


def _resources(args) -> int:
    from saf.runtime.stats import ResourceStats

    registry = build_resolver().registry
    out = {"resources": [{"resource_id": r.resource_id,
                          "capabilities": list(r.capability_ids),
                          "trust": float(getattr(r, "trust", 0.0))}
                         for r in registry.all()]}
    if args.stats:
        out["observed"] = ResourceStats(f"{args.state_dir}/resource-stats.jsonl").summary()
    _print(out)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="saf", description="Sovereign Agent Fabric")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("doctor")
    sub.add_parser("capabilities")

    res = sub.add_parser("resources", help="list resources and observed reliability")
    res.add_argument("--stats", action="store_true")
    res.add_argument("--state-dir", default=".saf")

    r = sub.add_parser("run", help="compile + rank a plan, optionally record it on the kernel")
    r.add_argument("intent")
    r.add_argument("--es", help="effective-scale-OS base URL, e.g. http://127.0.0.1:8080")
    r.add_argument("--token", help="bearer token for the kernel API")
    r.add_argument("--namespace", default="default")
    r.add_argument("--state-dir", default=".saf")
    r.add_argument("--no-queue", action="store_true",
                   help="fail instead of queueing in the offline outbox")

    e = sub.add_parser("execute", help="execute an intent through ranked capabilities")
    e.add_argument("intent")
    e.add_argument("--workspace", default=".")
    e.add_argument("--state-dir", default=".saf")
    e.add_argument("--test-command", default=None,
                   help="shell-quoted argv for cap://software/testing/execute "
                        "(default: python3 -m pytest -q); never passed to a shell")

    w = sub.add_parser("worker", help="run a lease-bound worker against the kernel")
    w.add_argument("--es", required=True)
    w.add_argument("--token")
    w.add_argument("--namespace", default="default")
    w.add_argument("--worker-id")
    w.add_argument("--workspace", default=".")
    w.add_argument("--state-dir", default=".saf")
    w.add_argument("--claim-ttl", type=float, default=60.0)
    w.add_argument("--poll-interval", type=float, default=1.0)
    w.add_argument("--max-jobs", type=int)
    w.add_argument("--idle-limit", type=int, help="stop after N idle polls")
    w.add_argument("--once", action="store_true", help="one poll, then exit")
    w.add_argument("--test-command", default=None,
                   help="shell-quoted argv for cap://software/testing/execute")

    s = sub.add_parser("sync", help="reconcile the offline outbox with the kernel")
    s.add_argument("--es", required=True)
    s.add_argument("--token")
    s.add_argument("--namespace", default="default")
    s.add_argument("--state-dir", default=".saf")
    s.add_argument("--limit", type=int, default=50)

    v = sub.add_parser("verify", help="verify the evidence hash chain")
    v.add_argument("--state-dir", default=".saf")

    l = sub.add_parser("ledger", help="show the evidence ledger tail")
    l.add_argument("--state-dir", default=".saf")
    l.add_argument("--limit", type=int, default=20)

    rb = sub.add_parser("rollback", help="restore a rollback point (snapshot -> restore)")
    rb.add_argument("execution_id", nargs="?", default="latest")
    rb.add_argument("--workspace", default=None)
    rb.add_argument("--state-dir", default=".saf")
    rb.add_argument("--dry-run", action="store_true")

    a = p.parse_args()
    if a.cmd == "doctor":
        _print({"status": "ok", "python": platform.python_version(),
                "platform": platform.platform()})
    elif a.cmd == "capabilities":
        for x in build_resolver().registry.all():
            print(f"{x.resource_id}: {', '.join(x.capability_ids)}")
    elif a.cmd == "resources":
        sys.exit(_resources(a))
    elif a.cmd == "run":
        _print(_local_run(a.intent, a.es, a.token, a.namespace, a.state_dir, not a.no_queue))
    elif a.cmd == "execute":
        sys.exit(_execute(a.intent, a))
    elif a.cmd == "worker":
        sys.exit(_worker(a))
    elif a.cmd == "sync":
        sys.exit(_sync(a))
    elif a.cmd == "verify":
        sys.exit(_verify(a))
    elif a.cmd == "ledger":
        sys.exit(_ledger(a))
    elif a.cmd == "rollback":
        sys.exit(_rollback(a))
    else:
        p.print_help()


if __name__ == "__main__":
    main()

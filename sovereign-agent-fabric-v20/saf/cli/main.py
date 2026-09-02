import argparse
import json
import platform

from saf.core.compiler import compile_intent
from saf.core.contracts import ExecutionContext, Task
from saf.core.policy import PolicyEngine
from saf.runtime.bootstrap import build_resolver


def _local_run(intent: str, es: str | None, token: str | None, namespace: str) -> dict:
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
    if es:
        import asyncio

        from saf.transport.effective_scale import EffectiveScaleTransport, TransportError

        transport = EffectiveScaleTransport(es, token=token, namespace=namespace)
        try:
            submitted = asyncio.run(transport.submit(task))
            out["execution_plan"] = {
                "workflow_id": submitted["workflow"]["id"],
                "status": submitted["workflow"]["status"],
                "idempotency_key": submitted["idempotency_key"],
                "replayed": submitted["replayed"],
            }
        except TransportError as exc:
            out["execution_plan"] = {
                "status": "unavailable",
                "error": str(exc),
                "code": exc.code,
                "hint": "kernel unreachable — submit without --es for local mode",
            }
    return out


def main():
    p = argparse.ArgumentParser(prog="saf")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor")
    sub.add_parser("capabilities")
    r = sub.add_parser("run")
    r.add_argument("intent")
    r.add_argument("--es", help="effective-scale-OS base URL, e.g. http://127.0.0.1:8080")
    r.add_argument("--token", help="bearer token for the kernel API")
    r.add_argument("--namespace", default="default")
    a = p.parse_args()
    if a.cmd == "doctor":
        print(json.dumps(
            {"status": "ok", "python": platform.python_version(),
             "platform": platform.platform()}, indent=2))
    elif a.cmd == "capabilities":
        for x in build_resolver().registry.all():
            print(f"{x.resource_id}: {', '.join(x.capability_ids)}")
    elif a.cmd == "run":
        print(json.dumps(_local_run(a.intent, a.es, a.token, a.namespace), indent=2))
    else:
        p.print_help()


if __name__ == "__main__":
    main()

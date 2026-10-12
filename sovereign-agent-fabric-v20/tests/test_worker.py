"""Lease-bound worker loop: claim -> execute -> heartbeat -> complete (ADR-006)."""
import asyncio
import json

from saf.core.compiler import compile_intent
from saf.core.contracts import AgentResult, ExecutionResult, StepResult
from saf.runtime.worker import KernelWorker
from saf.transport.effective_scale import TransportError


class StubTransport:
    """Kernel stand-in: scripted attempts, records every worker call."""

    def __init__(self, attempts=None, *, claim_error=None, complete_error=None):
        self.attempts = list(attempts or [])
        self.claim_error = claim_error
        self.complete_error = complete_error
        self.calls = []

    async def list_attempts(self, **kw):
        self.calls.append(("list", kw))
        return [dict(a) for a in self.attempts if a.get("claimable")]

    async def claim_attempt(self, attempt_id, *, worker_id, ttl_seconds=None):
        self.calls.append(("claim", attempt_id, worker_id, ttl_seconds))
        if self.claim_error:
            raise self.claim_error
        for a in self.attempts:
            if a["id"] == attempt_id:
                a["claimable"] = False
        return {"nonce": "nonce-1", "deadline": None, "attempt": {"id": attempt_id}}

    async def heartbeat_attempt(self, attempt_id, *, worker_id, nonce, ttl_seconds=None):
        self.calls.append(("heartbeat", attempt_id, nonce))
        return {"deadline": None}

    async def complete_attempt(self, attempt_id, *, ok, worker_id, nonce, error=None, result=None):
        self.calls.append(("complete", attempt_id, ok, error, result))
        if self.complete_error:
            raise self.complete_error
        return {"ok": True, "idempotent": False}


class StubExecutor:
    """Executor stand-in returning a canned ExecutionResult."""

    workspace = "/tmp/ws"
    platform = "test"

    def __init__(self, ok=True, *, delay=0.0, raises=None):
        self.ok = ok
        self.delay = delay
        self.raises = raises
        self.tasks = []

    async def execute(self, task, *, context=None, execution_id=None):
        self.tasks.append(task)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        step = StepResult(capability=task.required_capabilities[0],
                          status="succeeded" if self.ok else "failed",
                          resource_id="saf://local", summary="ran", verified=True)
        return ExecutionResult(execution_id="exec-1", intent=task.intent, ok=self.ok,
                               status="succeeded" if self.ok else "failed", steps=[step],
                               evidence_hash="e" * 64)


def attempt(**kw):
    base = {"id": "a-1", "workflow_id": "wf-1", "node_id": "cap-0",
            "node_name": "cap://software/testing/execute", "claimable": True,
            "workflow_name": "saf/task", "status": "running"}
    base.update(kw)
    return base


def worker(transport, executor, **kw):
    kw.setdefault("claim_ttl", 30.0)
    kw.setdefault("poll_interval", 0.01)
    return KernelWorker(transport, executor, worker_id="saf-test", **kw)


def test_no_work_returns_none_and_does_not_claim():
    transport, executor = StubTransport([]), StubExecutor()
    assert asyncio.run(worker(transport, executor).run_once()) is None
    assert [c[0] for c in transport.calls] == ["list"]


def test_happy_path_claims_executes_and_completes_with_evidence():
    transport, executor = StubTransport([attempt()]), StubExecutor()
    record = asyncio.run(worker(transport, executor).run_once())

    assert record["status"] == "succeeded" and record["ok"]
    kinds = [c[0] for c in transport.calls]
    assert kinds == ["list", "claim", "complete"]
    _, attempt_id, ok, error, result = transport.calls[-1]
    assert attempt_id == "a-1" and ok is True and error is None
    assert result["evidence_hash"] == "e" * 64
    assert result["worker_id"] == "saf-test" and result["resource_id"] == "saf://local"
    assert executor.tasks[0].required_capabilities == ["cap://software/testing/execute"]
    assert executor.tasks[0].constraints["attempt_id"] == "a-1"


def test_failure_is_completed_as_a_failure_with_a_reason():
    transport, executor = StubTransport([attempt()]), StubExecutor(ok=False)
    record = asyncio.run(worker(transport, executor).run_once())
    _, _, ok, error, result = transport.calls[-1]
    assert not ok and error and record["status"] == "failed"
    assert result["status"] == "failed"


def test_lost_claim_race_is_reported_not_retried():
    transport = StubTransport([attempt()], claim_error=TransportError(
        "attempt already claimed by w2", status=409))
    record = asyncio.run(worker(transport, StubExecutor()).run_once())
    assert record == {"attempt_id": "a-1", "status": "lost_race",
                      "error": "attempt already claimed by w2"}
    assert [c[0] for c in transport.calls] == ["list", "claim"]


def test_fenced_completion_is_reported():
    transport = StubTransport([attempt()], complete_error=TransportError(
        "attempt is fenced", status=409))
    record = asyncio.run(worker(transport, StubExecutor()).run_once())
    assert record["status"] == "fenced" and record["ok"]


def test_executor_exception_never_kills_the_worker_loop():
    transport = StubTransport([attempt()]), None
    transport = StubTransport([attempt()])
    record = asyncio.run(worker(transport, StubExecutor(raises=RuntimeError("boom"))).run_once())
    assert not record["ok"] and "boom" in record["error"]
    assert transport.calls[-1][0] == "complete"


def test_deadline_exceeded_completes_early(monkeypatch):
    transport = StubTransport([attempt()])

    async def short_deadline(self, attempt_id, *, worker_id, ttl_seconds=None):
        return {"nonce": "nonce-1", "deadline": __import__("time").time() + 1.0}

    monkeypatch.setattr(StubTransport, "claim_attempt", short_deadline)
    record = asyncio.run(worker(transport, StubExecutor(delay=3.0)).run_once())
    assert not record["ok"] and record["error"] == "deadline_exceeded"
    assert transport.calls[-1][0] == "complete"


def test_serve_stops_after_idle_limit_and_reports_kernel_outage():
    quiet = asyncio.run(worker(StubTransport([]), StubExecutor()).serve(idle_limit=2))
    assert quiet["jobs"] == 0 and quiet["status"] == "ok" and quiet["idle_rounds"] == 2

    class Dead(StubTransport):
        async def list_attempts(self, **kw):
            raise TransportError("effective-scale-OS unreachable: refused")

    out = asyncio.run(worker(Dead([]), StubExecutor()).serve(max_jobs=1))
    assert out["status"] == "kernel_unavailable" and "unreachable" in out["error"]


def test_serve_drains_multiple_jobs():
    transport = StubTransport([attempt(id="a-1"), attempt(id="a-2", node_id="cap-1")])
    out = asyncio.run(worker(transport, StubExecutor()).serve(max_jobs=2))
    assert out["jobs"] == 2 and out["succeeded"] == 2 and out["status"] == "ok"
    assert [r["status"] for r in out["records"]] == ["succeeded", "succeeded"]

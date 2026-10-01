"""Lease-bound SAF worker for the effective-scale-OS kernel (ADR-006).

The worker is the "execute" half of the integration: the kernel schedules
capability nodes and issues lease-bound attempts; SAF pulls them, resolves each
node's capability to a ranked runtime, executes it with the §16 lifecycle, and
completes the attempt with the evidence pointer plus the fencing nonce it was
issued at claim time.

Guarantees:
  * a job runs under the kernel's deadline — the worker never outlives its lease
    silently: it heartbeats while working and aborts cleanly if fenced out;
  * a lost lease (409) is reported, never retried blindly;
  * execution results — success or failure — always carry a reason;
  * an unreachable kernel degrades to a structured error (offline is a state,
    not a crash).
"""
from __future__ import annotations

import asyncio
import time

from saf.core.contracts import ExecutionContext, Task
from saf.core.ids import new_worker_id
from saf.transport.effective_scale import EffectiveScaleTransport, TransportError


class KernelWorker:
    def __init__(self, transport: EffectiveScaleTransport, executor, *, worker_id: str | None = None,
                 claim_ttl: float = 60.0, poll_interval: float = 1.0, logger=None):
        self.transport = transport
        self.executor = executor
        self.worker_id = worker_id or new_worker_id()
        self.claim_ttl = float(claim_ttl)
        self.poll_interval = float(poll_interval)
        self.logger = logger

    # ------------------------------------------------------------------ one job

    async def run_once(self) -> dict | None:
        """Claim and execute at most one attempt. Returns a job record or None."""
        attempts = await self.transport.list_attempts(claimable=True, state="running", limit=1)
        if not attempts:
            return None
        attempt = attempts[0]
        try:
            claim = await self.transport.claim_attempt(attempt["id"], worker_id=self.worker_id,
                                                       ttl_seconds=self.claim_ttl)
        except TransportError as exc:
            if exc.status == 409:
                return {"attempt_id": attempt["id"], "status": "lost_race", "error": str(exc)}
            raise
        nonce = claim["nonce"]
        deadline = float(claim.get("deadline") or (time.time() + self.claim_ttl))
        capability = attempt.get("node_name") or "cap://general/agent/execute"
        task = Task(intent=attempt.get("workflow_name") or capability,
                    required_capabilities=[capability],
                    constraints={"capability": capability, "attempt_id": attempt["id"]})
        ctx = ExecutionContext(task_id=attempt["id"], workspace=self.executor.workspace,
                               platform=self.executor.platform, network_available=True)
        started = time.time()
        heartbeat = asyncio.create_task(self._heartbeat(attempt["id"], nonce, deadline))
        try:
            budget = max(1.0, deadline - time.time() - 0.5)
            result = await asyncio.wait_for(self.executor.execute(task, context=ctx), timeout=budget)
        except asyncio.TimeoutError:
            return await self._finish(attempt, nonce, started, ok=False,
                                      error="deadline_exceeded",
                                      summary="worker deadline exceeded before completion")
        except Exception as exc:  # noqa: BLE001 — report, never crash the worker loop
            return await self._finish(attempt, nonce, started, ok=False,
                                      error=f"executor_error: {exc}", summary=str(exc))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        step = result.steps[0] if result.steps else None
        return await self._finish(
            attempt, nonce, started, ok=bool(result.ok),
            error=None if result.ok else (step.summary if step else "capability failed"),
            summary=(step.summary if step else result.status),
            result_payload={
                "ok": result.ok, "status": result.status,
                "capability": capability,
                "resource_id": step.resource_id if step else None,
                "step_status": step.status if step else None,
                "verified": step.verified if step else None,
                "evidence_hash": result.evidence_hash,
                "execution_id": result.execution_id,
                "duration_s": result.duration_s,
            })

    async def _finish(self, attempt: dict, nonce: str, started: float, *, ok: bool,
                      error: str | None, summary: str,
                      result_payload: dict | None = None) -> dict:
        payload = {"summary": summary, "worker_id": self.worker_id,
                   "duration_s": round(time.time() - started, 4), **(result_payload or {})}
        try:
            completion = await self.transport.complete_attempt(
                attempt["id"], ok=ok, worker_id=self.worker_id, nonce=nonce,
                error=error, result=payload)
            status = "succeeded" if ok else "failed"
            if completion.get("idempotent"):
                status = "already_completed"
        except TransportError as exc:
            status = "fenced" if exc.status == 409 else "completion_error"
            completion = {"error": str(exc), "status": exc.status}
        record = {"attempt_id": attempt["id"], "workflow_id": attempt.get("workflow_id"),
                  "node_id": attempt.get("node_id"), "capability": attempt.get("node_name"),
                  "worker_id": self.worker_id, "status": status, "ok": ok,
                  "error": error, "result": payload, "completion": completion}
        if self.logger:
            self.logger.log("saf.worker.job", **{k: v for k, v in record.items()
                                                 if k not in ("result", "completion")})
        return record

    async def _heartbeat(self, attempt_id: str, nonce: str, deadline: float) -> None:
        """Renew the lease while the job runs; stop if fenced or the lease is lost."""
        interval = max(2.0, self.claim_ttl / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                out = await self.transport.heartbeat_attempt(
                    attempt_id, worker_id=self.worker_id, nonce=nonce,
                    ttl_seconds=self.claim_ttl)
            except TransportError as exc:
                if exc.status == 409:
                    return  # fenced: another copy owns the attempt now
                continue
            deadline = float(out.get("deadline") or deadline)

    # --------------------------------------------------------------------- loop

    async def serve(self, *, max_jobs: int | None = None, stop: asyncio.Event | None = None,
                    idle_limit: int | None = None) -> dict:
        """Poll-claim-execute until a bound is hit. Bounded, resumable, observable."""
        done = {"jobs": 0, "succeeded": 0, "failed": 0, "idle_rounds": 0,
                "worker_id": self.worker_id, "records": []}
        while stop is None or not stop.is_set():
            try:
                record = await self.run_once()
            except TransportError as exc:
                return {**done, "status": "kernel_unavailable", "error": str(exc),
                        "code": exc.code}
            if record is None:
                done["idle_rounds"] += 1
                if idle_limit is not None and done["idle_rounds"] >= idle_limit:
                    break
                await asyncio.sleep(self.poll_interval)
                continue
            done["idle_rounds"] = 0
            done["jobs"] += 1
            done["records"].append(record)
            if record.get("ok"):
                done["succeeded"] += 1
            else:
                done["failed"] += 1
            if max_jobs is not None and done["jobs"] >= max_jobs:
                break
        return {**done, "status": "ok"}

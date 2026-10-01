"""Capability execution: PLAN -> EXECUTE -> RESULT -> VALIDATE -> EVIDENCE.

This is the slice the a review record called out as missing: until now the
resolver produced a *plan*; nothing executed it. `CapabilityExecutor` closes the
§16 lifecycle for one task:

  plan      policy gate, deterministic resolution (fit/trust/cost/environment)
  snapshot  rollback point for mutating work (saf/tools/backup.py)
  execute   ranked candidate fall-through: unsupported/unavailable candidates
            are skipped, real failures stop the step
  validate  mutating steps are proven against on-disk state (save-proof), not
            against what the agent *claims* (docs §16: "Agent claims are not
            evidence") and can be rolled back
  evidence  every step is appended to the hash-chained ledger; the final record
            hash is the execution's evidence pointer
  memory    a durable run summary is remembered (world-model update)

Bounded by construction: adapters own their timeouts, argv is adapter-owned, and
a failing candidate never raises into the caller.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from saf.agents.local import UNSUPPORTED
from saf.core.contracts import (AgentRequest, ExecutionContext, ExecutionResult,
                                StepResult, Task)
from saf.core.ids import new_execution_id
from saf.core.policy import PolicyEngine

# Capabilities that may mutate the filesystem: these get a rollback point.
MUTATING_CAPABILITIES = {
    "cap://software/code/refactor",
    "cap://software/git/operate",
    "cap://software/agent/execute",
}

_FALL_THROUGH = {"unsupported", "unavailable"}


class CapabilityExecutor:
    def __init__(self, resolver, *, workspace: str | Path = ".", ledger, verifier=None,
                 memory=None, policy=None, backups=None, platform: str = "unknown",
                 resource_stats=None):
        self.resolver = resolver
        self.workspace = str(Path(workspace).resolve())
        self.ledger = ledger
        self.verifier = verifier
        self.memory = memory
        self.policy = policy or PolicyEngine()
        self.backups = backups
        self.platform = platform
        self.resource_stats = resource_stats

    async def execute(self, task: Task, *, context: ExecutionContext | None = None,
                      execution_id: str | None = None) -> ExecutionResult:
        execution_id = execution_id or new_execution_id()
        ctx = context or ExecutionContext(task_id=execution_id, workspace=self.workspace,
                                          platform=self.platform)
        ctx = ctx.model_copy(update={"task_id": execution_id, "workspace": self.workspace})
        started = time.time()
        caps = list(dict.fromkeys(task.required_capabilities))
        result = ExecutionResult(execution_id=execution_id, intent=task.intent, ok=False,
                                 status="failed", capabilities=caps, workspace=self.workspace,
                                 started_at=started)

        allowed, reason = self.policy.authorize(task)
        result.policy = {"allowed": allowed, "reason": reason,
                         "autonomy": task.autonomy.value}
        if not allowed:
            result.status = "blocked"
            result.finished_at = time.time()
            result.duration_s = round(result.finished_at - started, 4)
            self.ledger.append({"event": "execution.blocked", "execution_id": execution_id,
                                "intent": task.intent, "reason": reason,
                                "capabilities": caps})
            return result

        ranked = self.resolver.resolve(task, ctx)
        mutating = self._is_mutating(task, caps)
        if mutating and self.backups is not None:
            point = self.backups.create_point(execution_id, self.workspace,
                                              paths=task.constraints.get("targets") or None)
            result.rollback_point = execution_id
            self.ledger.append({"event": "execution.rollback_point", "execution_id": execution_id,
                                "files": len(point.get("entries", [])),
                                "stored": point.get("stored", 0)})

        for capability in caps:
            step = await self._run_capability(capability, task, ctx, ranked, execution_id)
            result.steps.append(step)

        result.ok = all(s.status == "succeeded" for s in result.steps)
        result.status = ("succeeded" if result.ok else
                         "blocked" if result.status == "blocked" else
                         "partial" if any(s.status == "succeeded" for s in result.steps) else "failed")
        result.finished_at = time.time()
        result.duration_s = round(result.finished_at - started, 4)
        record = self.ledger.append({
            "event": "execution.result", "execution_id": execution_id, "intent": task.intent,
            "ok": result.ok, "status": result.status, "capabilities": caps,
            "steps": [{"capability": s.capability, "status": s.status,
                       "resource_id": s.resource_id, "verified": s.verified} for s in result.steps],
            "rollback_point": result.rollback_point, "duration_s": result.duration_s,
        })
        result.evidence_hash = record.get("hash")
        if self.memory is not None:
            self.memory.remember("run-summary", {
                "execution_id": execution_id, "intent": task.intent, "ok": result.ok,
                "status": result.status,
                "steps": [{"capability": s.capability, "status": s.status,
                           "resource": s.resource_id} for s in result.steps],
                "evidence_hash": result.evidence_hash,
            }, provenance={"source": "CapabilityExecutor", "workspace": self.workspace})
        return result

    # ------------------------------------------------------------- one capability

    async def _run_capability(self, capability: str, task: Task, ctx: ExecutionContext,
                              ranked: list, execution_id: str) -> StepResult:
        started = time.time()
        step = StepResult(capability=capability, status="failed")
        candidates = [c for c in ranked if capability in c.capabilities] or self._fallback(ranked)
        if not candidates:
            step.status = "unavailable"
            step.summary = "no registered resource claims this capability"
            self._record_step(step, execution_id, started)
            return step

        for candidate in candidates:
            resource = self._resource(candidate.resource_id)
            if resource is None:
                step.attempts.append({"resource_id": candidate.resource_id,
                                      "status": "unavailable", "reason": "not in registry"})
                continue
            request = self._request(task, ctx, capability)
            before = self._snapshot_targets(task)
            try:
                outcome = await resource.execute(request)
            except Exception as exc:  # noqa: BLE001 — a runtime must never break the fabric
                step.attempts.append({"resource_id": candidate.resource_id, "status": "failed",
                                      "reason": f"runtime raised: {exc}"})
                continue
            self._count(candidate.resource_id, outcome.ok)

            if outcome.ok:
                step.resource_id = candidate.resource_id
                step.status = "succeeded"
                step.summary = outcome.summary
                step.artifacts = list(outcome.artifacts)
                step.evidence = list(outcome.evidence)
                if capability in MUTATING_CAPABILITIES or task.constraints.get("mutating"):
                    step.verified = self._verify(task, before, capability, execution_id)
                break

            reason = self._failure_reason(outcome)
            step.attempts.append({"resource_id": candidate.resource_id, "status": reason,
                                  "reason": outcome.summary})
            if reason in _FALL_THROUGH:
                continue
            step.resource_id = candidate.resource_id
            step.status = "failed"
            step.summary = outcome.summary
            break
        else:
            step.status = "unavailable"
            step.summary = "every candidate was unsupported or unavailable: " + \
                "; ".join(f"{a['resource_id']}: {a['status']}" for a in step.attempts)

        self._record_step(step, execution_id, started)
        return step

    # ------------------------------------------------------------------ helpers

    def _resource(self, resource_id: str):
        try:
            return self.resolver.registry.get(resource_id)
        except KeyError:
            return None

    def _fallback(self, ranked: list) -> list:
        """No resource claims the capability: try the general-purpose agents."""
        return [c for c in ranked if "cap://software/agent/execute" in c.capabilities]

    def _request(self, task: Task, ctx: ExecutionContext, capability: str) -> AgentRequest:
        step_task = task.model_copy(update={
            "required_capabilities": [capability],
            "constraints": {**task.constraints, "capability": capability},
        })
        prompt = (f"Capability: {capability}\nIntent: {task.intent}\n"
                  f"Workspace: {ctx.workspace}")
        return AgentRequest(task=step_task, context=ctx, prompt=prompt)

    def _snapshot_targets(self, task: Task) -> dict:
        from saf.tools.filesystem import snapshot

        targets = task.constraints.get("targets") or []
        return {t: snapshot(Path(self.workspace) / t if not Path(t).is_absolute() else t)
                for t in targets}

    def _verify(self, task: Task, before: dict, capability: str, execution_id: str) -> bool | None:
        """Save-proof: prove mutation from disk state; ledger the verdict."""
        if self.verifier is None:
            return None
        targets = task.constraints.get("targets") or []
        if not targets:
            self.ledger.append({"event": "save-proof", "execution_id": execution_id,
                                "capability": capability, "verified": None,
                                "reason": "no targets declared — nothing to prove"})
            return None
        expected = task.constraints.get("expected_hash")
        verdicts = []
        for target in targets:
            path = Path(target) if Path(target).is_absolute() else Path(self.workspace) / target
            proof = self.verifier.prove_saved(before.get(target), path,
                                              expected_hash=expected if len(targets) == 1 else None)
            verdicts.append(bool(proof.get("verified")))
        return all(verdicts)

    @staticmethod
    def _failure_reason(outcome) -> str:
        for item in outcome.evidence or []:
            if isinstance(item, dict) and item.get("kind") == "unsupported":
                return "unsupported"
        text = f"{outcome.summary} {outcome.stderr}".lower()
        if "not installed" in text or "not on path" in text or "failed to start" in text:
            return "unavailable"
        return "failed"

    def _record_step(self, step: StepResult, execution_id: str, started: float) -> None:
        step.duration_s = round(time.time() - started, 4)
        self.ledger.append({
            "event": "execution.step", "execution_id": execution_id, "capability": step.capability,
            "status": step.status, "resource_id": step.resource_id, "summary": step.summary,
            "duration_s": step.duration_s, "verified": step.verified,
            "attempts": step.attempts, "artifacts": step.artifacts,
        })

    def _count(self, resource_id: str, ok: bool) -> None:
        if self.resource_stats is not None:
            self.resource_stats.record(resource_id, ok)

    @staticmethod
    def _is_mutating(task: Task, caps: list[str]) -> bool:
        if task.constraints.get("mutating"):
            return True
        return any(c in MUTATING_CAPABILITIES for c in caps)


async def execute_intent(intent: str, *, executor: CapabilityExecutor) -> ExecutionResult:
    """Compile an intent and execute it (the CLI path)."""
    from saf.core.compiler import compile_intent

    return await executor.execute(compile_intent(intent))

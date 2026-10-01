"""Execution slice: local runtime, candidate fall-through, save-proof, evidence."""
import asyncio
import json
from pathlib import Path

import pytest

from saf.agents.base import AgentRuntime
from saf.agents.local import UNSUPPORTED, LocalRuntime
from saf.core.compiler import compile_intent
from saf.core.contracts import AgentResult, AgentRequest, Autonomy, ExecutionContext, Task
from saf.core.registry import Registry
from saf.core.resolver import CapabilityResolver
from saf.evidence.ledger import EvidenceLedger
from saf.evidence.verification import VerificationEngine
from saf.memory.store import MemoryStore
from saf.runtime.executor import CapabilityExecutor
from saf.runtime.stats import ResourceStats
from saf.tools.backup import BackupStore


def run(coro):
    return asyncio.run(coro)


def make_executor(tmp_path, registry=None, **kw):
    registry = registry or Registry()
    if not registry.ids():
        registry.register("saf://local", LocalRuntime(test_command=kw.pop("test_command", None)))
    ledger = EvidenceLedger(str(tmp_path / "evidence.jsonl"))
    return CapabilityExecutor(
        CapabilityResolver(registry), workspace=str(tmp_path / "ws"),
        ledger=ledger, verifier=VerificationEngine(ledger),
        memory=MemoryStore(str(tmp_path / "memory.jsonl")),
        backups=BackupStore(str(tmp_path / "backups")),
        resource_stats=ResourceStats(str(tmp_path / "stats.jsonl")),
        **kw,
    )


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "mod.py").write_text("print(1)\n")
    (ws / "README.md").write_text("hello\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "junk.js").write_text("noise")
    return ws


class ScriptedRuntime(AgentRuntime):
    """Test double: returns queued results, records requests."""

    def __init__(self, resource_id, capability_ids, results):
        self.resource_id = resource_id
        self.kind = "agent"
        self.capability_ids = list(capability_ids)
        self.trust = 0.5
        self.cost_estimate = 0.0
        self.latency_estimate_ms = 10
        self._results = list(results)
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return self._results.pop(0) if self._results else AgentResult(ok=True, summary="default")


# ------------------------------------------------------------------ local runtime


def test_local_inventory_is_deterministic_and_skips_noise(tmp_path):
    ws = workspace(tmp_path)
    runtime = LocalRuntime()
    request = _request("cap://software/repository/inspect", ws)

    first = run(runtime.execute(request))
    second = run(runtime.execute(request))

    assert first.ok and "2 files" in first.summary
    assert first.evidence == second.evidence
    assert first.evidence[0]["extensions"] == {".py": 1, ".md": 1}
    assert first.evidence[0]["truncated"] is False


def test_local_test_command_maps_exit_code_and_timeout(tmp_path):
    ws = workspace(tmp_path)
    ok = run(LocalRuntime(test_command=["python3", "-c", "print('green')"]).execute(
        _request("cap://software/testing/execute", ws)))
    assert ok.ok and "green" in ok.summary and ok.evidence[0]["exit_code"] == 0

    bad = run(LocalRuntime(test_command=["python3", "-c", "raise SystemExit(3)"]).execute(
        _request("cap://software/testing/execute", ws)))
    assert not bad.ok and bad.evidence[0]["exit_code"] == 3

    slow = run(LocalRuntime(test_command=["python3", "-c", "import time; time.sleep(5)"],
                            timeout_s=0.4).execute(_request("cap://software/testing/execute", ws)))
    assert not slow.ok and "timed out" in slow.summary


def test_local_unsupported_capability_is_a_structured_marker(tmp_path):
    ws = workspace(tmp_path)
    out = run(LocalRuntime().execute(_request("cap://software/code/refactor", ws)))
    assert not out.ok
    assert UNSUPPORTED in out.summary
    assert out.evidence[0]["kind"] == "unsupported"


def test_local_save_proof_detects_hash_mismatch(tmp_path):
    ws = workspace(tmp_path)
    target = ws / "README.md"
    good = _request("cap://verification/save-proof", ws,
                    constraints={"targets": ["README.md"], "expected_hash": _sha(target)})
    assert run(LocalRuntime().execute(good)).ok
    bad = _request("cap://verification/save-proof", ws,
                   constraints={"targets": ["README.md"], "expected_hash": "0" * 64})
    out = run(LocalRuntime().execute(bad))
    assert not out.ok and out.evidence[0]["targets"][0]["expected_hash_ok"] is False


# ---------------------------------------------------------------------- executor


def test_executor_runs_full_lifecycle_and_records_evidence(tmp_path):
    workspace(tmp_path)
    executor = make_executor(tmp_path, test_command=["python3", "-c", "print('ok')"])
    result = run(executor.execute(compile_intent("inspect repository and run tests")))

    assert result.ok and result.status == "succeeded"
    assert [s.status for s in result.steps] == ["succeeded", "succeeded"]
    assert all(s.resource_id == "saf://local" for s in result.steps)
    assert result.evidence_hash and len(result.evidence_hash) == 64

    ledger = EvidenceLedger(str(tmp_path / "evidence.jsonl"))
    assert ledger.verify()["ok"]
    events = [r["event"] for r in ledger.all()]
    assert events.count("execution.step") == 2 and events[-1] == "execution.result"

    memory = MemoryStore(str(tmp_path / "memory.jsonl")).search("run-summary")
    assert len(memory) == 1 and memory[0]["content"]["status"] == "succeeded"
    assert ResourceStats(str(tmp_path / "stats.jsonl")).summary()[0]["resource_id"] == "saf://local"


def test_executor_falls_through_unsupported_candidates(tmp_path):
    workspace(tmp_path)
    unsupported = ScriptedRuntime("saf://stub-a", ["cap://software/testing/execute"],
                                  [AgentResult(ok=False, summary=f"{UNSUPPORTED}: nope",
                                               evidence=[{"kind": "unsupported"}])])
    winner = ScriptedRuntime("saf://stub-b", ["cap://software/testing/execute"],
                             [AgentResult(ok=True, summary="ran the tests")])
    registry = Registry()
    registry.register(unsupported.resource_id, unsupported)
    registry.register(winner.resource_id, winner)
    executor = make_executor(tmp_path, registry=registry)

    result = run(executor.execute(Task(intent="run tests",
                                       required_capabilities=["cap://software/testing/execute"])))
    step = result.steps[0]
    assert step.status == "succeeded" and step.resource_id == "saf://stub-b"
    assert step.attempts[0]["status"] == "unsupported"
    assert winner.requests[0].task.constraints["capability"] == "cap://software/testing/execute"


def test_executor_reports_unavailable_when_nothing_can_run(tmp_path):
    workspace(tmp_path)
    registry = Registry()
    registry.register("saf://stub", ScriptedRuntime(
        "saf://stub", ["cap://software/code/refactor"],
        [AgentResult(ok=False, summary="cursor is not installed or not on PATH.")]))
    executor = make_executor(tmp_path, registry=registry)
    result = run(executor.execute(Task(intent="refactor code",
                                       required_capabilities=["cap://software/code/refactor"])))
    assert not result.ok and result.steps[0].status == "unavailable"
    assert result.steps[0].attempts[0]["status"] == "unavailable"


def test_executor_blocks_destructive_intent_before_execution(tmp_path):
    workspace(tmp_path)
    executor = make_executor(tmp_path)
    task = compile_intent("delete production database")
    task = task.model_copy(update={"autonomy": Autonomy.A3})
    result = run(executor.execute(task))
    assert result.status == "blocked" and not result.ok and result.steps == []
    ledger = EvidenceLedger(str(tmp_path / "evidence.jsonl"))
    assert [r["event"] for r in ledger.all()] == ["execution.blocked"]


def test_executor_creates_rollback_point_for_mutations(tmp_path):
    ws = workspace(tmp_path)
    executor = make_executor(tmp_path)
    task = Task(intent="refactor code", required_capabilities=["cap://software/code/refactor"],
                constraints={"targets": ["README.md"]})
    runtime = LocalRuntime()
    executor.resolver.registry.register("saf://stub-refactor", ScriptedRuntime(
        "saf://stub-refactor", ["cap://software/code/refactor"], [AgentResult(ok=True, summary="ok")]))
    result = run(executor.execute(task))

    assert result.ok and result.rollback_point
    # the stub claimed success but changed nothing: save-proof must say so
    step = result.steps[0]
    assert step.verified is False

    # simulate the mutation, then roll back to the point
    (ws / "README.md").write_text("mutated\n")
    restore = BackupStore(str(tmp_path / "backups")).restore(result.rollback_point)
    assert restore["ok"] and restore["restored"] == ["README.md"]
    assert (ws / "README.md").read_text() == "hello\n"


class WritingRuntime(ScriptedRuntime):
    """Test double that performs a real mutation, so save-proof has something to prove."""

    def __init__(self, path: Path, content: str):
        super().__init__("saf://writer", ["cap://software/code/refactor"],
                         [AgentResult(ok=True, summary="wrote the file")])
        self.path, self.content = path, content

    async def execute(self, request):
        Path(self.path).write_text(self.content)
        return await super().execute(request)


def test_executor_verifies_expected_hash_save_proof(tmp_path):
    ws = workspace(tmp_path)
    content = "written by the runtime\n"
    executor = make_executor(tmp_path)
    executor.resolver.registry.register("saf://writer", WritingRuntime(ws / "out.txt", content))
    expected = _sha_text(content)

    task = Task(intent="refactor code", required_capabilities=["cap://software/code/refactor"],
                constraints={"targets": ["out.txt"], "expected_hash": expected})
    result = run(executor.execute(task))
    assert result.ok and result.steps[0].verified is True

    wrong = Task(intent="refactor code", required_capabilities=["cap://software/code/refactor"],
                 constraints={"targets": ["out.txt"], "expected_hash": "0" * 64})
    mismatch = run(executor.execute(wrong))
    assert mismatch.steps[0].verified is False


def test_executor_save_proof_without_expected_hash_needs_a_change(tmp_path):
    ws = workspace(tmp_path)
    executor = make_executor(tmp_path)
    executor.resolver.registry.register("saf://writer", WritingRuntime(ws / "out.txt", "v1\n"))
    task = Task(intent="refactor code", required_capabilities=["cap://software/code/refactor"],
                constraints={"targets": ["out.txt"]})
    result = run(executor.execute(task))
    assert result.steps[0].verified is True   # file was created by the mutation
    ledger = EvidenceLedger(str(tmp_path / "evidence.jsonl"))
    proofs = [r for r in ledger.all() if r["event"] == "save-proof"]
    assert proofs and proofs[0]["verified"] is True


# ------------------------------------------------------------------- helpers


def _request(capability, ws, constraints=None):
    task = Task(intent="t", required_capabilities=[capability],
                constraints={"capability": capability, **(constraints or {})})
    return AgentRequest(task=task, context=ExecutionContext(task_id="t", workspace=str(ws)),
                        prompt="p")


def _sha(path):
    return _sha_text(Path(path).read_bytes())


def _sha_text(text):
    import hashlib

    payload = text.encode() if isinstance(text, str) else text
    return hashlib.sha256(payload).hexdigest()

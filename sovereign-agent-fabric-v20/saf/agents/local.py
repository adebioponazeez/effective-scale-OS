"""Deterministic local capability runtime (`saf://local`).

Why this exists (README core law: *deterministic computation replaces
unnecessary LLM calls*):

The five CLI adapters cover code work that needs a model. But several canonical
capabilities are deterministic and must not burn a token or a subprocess farm:
inspecting a repository, running the test suite, and verifying a save-proof.
This runtime does exactly those three, in-process, with bounded inputs — and it
returns a structured *unsupported* marker for everything else so the executor can
fall through to an agent runtime instead of pretending.

Contract:
  * `AgentRequest.task.constraints["capability"]` names the capability to run;
  * `AgentRequest.context.workspace` is the only directory touched;
  * results are `AgentResult` — no exceptions escape, ever.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from .base import AgentRuntime
from saf.core.contracts import AgentResult
from saf.tools.filesystem import sha256_file

UNSUPPORTED = "unsupported_capability"

# Capabilities this runtime can satisfy without a model or a GUI agent.
LOCAL_CAPABILITIES = [
    "cap://software/repository/inspect",
    "cap://software/testing/execute",
    "cap://verification/save-proof",
]

# Directories never walked during inspection (noise + size).
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target"}


class LocalRuntime(AgentRuntime):
    """In-process, deterministic executor for inspect/test/save-proof."""

    resource_id = "saf://local"
    kind = "agent"
    capability_ids = LOCAL_CAPABILITIES
    trust = 0.6
    cost_estimate = 0.0
    latency_estimate_ms = 200

    def __init__(self, *, test_command: list[str] | None = None, timeout_s: float = 120.0,
                 max_files: int = 5000):
        self.test_command = list(test_command or ["python3", "-m", "pytest", "-q"])
        self.timeout_s = float(timeout_s)
        self.max_files = int(max_files)

    async def execute(self, request) -> AgentResult:
        capability = str(request.task.constraints.get("capability", ""))
        workspace = Path(request.context.workspace)
        if not workspace.is_dir():
            return AgentResult(ok=False, summary=f"workspace does not exist: {workspace}")
        if capability == "cap://software/repository/inspect":
            return self._inspect(workspace)
        if capability == "cap://software/testing/execute":
            return await self._run_tests(workspace)
        if capability == "cap://verification/save-proof":
            return self._save_proof(workspace, request.task.constraints)
        return AgentResult(
            ok=False,
            summary=f"{UNSUPPORTED}: saf://local cannot satisfy {capability or '<unset>'}",
            evidence=[{"kind": "unsupported", "capability": capability}],
        )

    # ------------------------------------------------------------- capabilities

    def _inspect(self, workspace: Path) -> AgentResult:
        """Deterministic inventory: bounded walk, extension histogram, VCS head."""
        counts: dict[str, int] = {}
        total = 0
        truncated = False
        for root, dirs, files in os.walk(workspace):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for name in sorted(files):
                if total >= self.max_files:
                    truncated = True
                    break
                total += 1
                ext = Path(name).suffix.lower() or "<none>"
                counts[ext] = counts.get(ext, 0) + 1
            if truncated:
                break
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        summary = (f"inspected {total} files"
                   f"{' (truncated)' if truncated else ''}; "
                   + ", ".join(f"{ext}:{n}" for ext, n in top))
        evidence = {
            "kind": "repository-inventory",
            "files": total,
            "truncated": truncated,
            "extensions": dict(top),
            "vcs_head": self._git_head(workspace),
        }
        return AgentResult(ok=True, summary=summary, evidence=[evidence])

    async def _run_tests(self, workspace: Path) -> AgentResult:
        """Run the configured command as a bounded, fixed-argv subprocess.

        `test_command` is adapter-owned configuration: user-controlled text never
        reaches argv, and there is no shell, so no flag/command injection.
        """
        if not self.test_command:
            return AgentResult(ok=False, summary="no test command configured")
        started = time.monotonic()
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self.test_command, cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE),
                timeout=self.timeout_s)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            return AgentResult(ok=False, summary=f"test command timed out after {self.timeout_s}s",
                               stderr="timeout")
        except (OSError, ValueError) as exc:
            return AgentResult(ok=False, summary=f"test command failed to start: {exc}")
        duration = time.monotonic() - started
        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace")
        tail = "\n".join(stdout.strip().splitlines()[-3:]) if stdout.strip() else ""
        return AgentResult(
            ok=proc.returncode == 0,
            summary=f"`{' '.join(self.test_command)}` exited {proc.returncode} in {duration:.2f}s"
                    + (f" :: {tail}" if tail else ""),
            stdout=stdout,
            stderr=stderr,
            evidence=[{"kind": "test-run", "command": list(self.test_command),
                       "exit_code": proc.returncode, "duration_s": round(duration, 4)}],
        )

    def _save_proof(self, workspace: Path, constraints: dict) -> AgentResult:
        """Verify declared targets: existence + content hashes (never claims)."""
        targets = constraints.get("targets") or []
        if not targets:
            return AgentResult(ok=False, summary="save-proof needs constraints.targets")
        expected = constraints.get("expected_hash")
        entries, ok = [], True
        for raw in targets:
            path = (workspace / raw) if not Path(raw).is_absolute() else Path(raw)
            exists = path.is_file()
            digest = sha256_file(path) if exists else None
            match = (digest == expected) if (expected and exists) else None
            if not exists or (expected is not None and match is False):
                ok = False
            entries.append({"path": str(raw), "exists": exists, "sha256": digest,
                            "expected_hash_ok": match})
        verified = sum(1 for e in entries if e["exists"])
        return AgentResult(
            ok=ok,
            summary=f"save-proof: {verified}/{len(entries)} targets present"
                    + (f", expected hash {'matched' if ok else 'MISMATCH'}" if expected else ""),
            artifacts=[e["path"] for e in entries if e["exists"]],
            evidence=[{"kind": "save-proof", "targets": entries, "expected_hash": expected}],
        )

    @staticmethod
    def _git_head(workspace: Path) -> str | None:
        """Read .git/HEAD without a subprocess (deterministic, read-only)."""
        head = workspace / ".git" / "HEAD"
        try:
            text = head.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("ref:"):
            ref = text.split(" ", 1)[1].strip()
            try:
                return (workspace / ".git" / ref).read_text(encoding="utf-8").strip()
            except OSError:
                return ref
        return text or None

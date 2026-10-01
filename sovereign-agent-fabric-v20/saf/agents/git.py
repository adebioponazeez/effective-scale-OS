"""Deterministic git runtime (`saf://git`) — `cap://software/git/operate`.

The compiler emits this capability for commit/repository intents; until now the executor
reported it unavailable. This runtime makes it real **without** handing an agent a shell:

  * every invocation is `git` + a fixed subcommand list, executed via
    `create_subprocess_exec` (no shell, no string interpolation);
  * the commit message is a *single argv element*, bounded in length, so quotes,
    backticks and semicolons are literal text — never syntax;
  * the only mutating operation is `add -A` + `commit -m <message>`, and it only runs when
    the caller supplied a message. Without one, the runtime reports read-only status;
  * a commit changes files through the working tree, so the executor's rollback point
    (BackupStore, which deliberately skips `.git/`) can restore file contents — but it does
    not rewind commit history. That limit is stated in docs/06-execution-lifecycle.md.

Contract: `AgentRequest.task.constraints` may carry
  `operation` ("status" | "commit"), `message` (string), `paths` (list of relative paths;
defaults to all changes), and `max_message_chars` (default 200, bounded to 2000).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .base import AgentRuntime
from saf.core.contracts import AgentResult

GIT_CAPABILITIES = ["cap://software/git/operate"]

DEFAULT_MAX_MESSAGE_CHARS = 200
HARD_MAX_MESSAGE_CHARS = 2000
OPERATIONS = {"status", "commit"}


class GitRuntime(AgentRuntime):
    """In-process git operations with a fixed vocabulary and bounded output."""

    resource_id = "saf://git"
    kind = "agent"
    capability_ids = GIT_CAPABILITIES
    trust = 0.65
    cost_estimate = 0.0
    latency_estimate_ms = 300

    def __init__(self, *, timeout_s: float = 30.0, max_output_chars: int = 20_000):
        self.timeout_s = float(timeout_s)
        self.max_output_chars = int(max_output_chars)

    async def execute(self, request) -> AgentResult:
        workspace = Path(request.context.workspace)
        if not workspace.is_dir():
            return AgentResult(ok=False, summary=f"workspace does not exist: {workspace}")

        constraints = dict(request.task.constraints or {})
        operation = str(constraints.get("operation") or "").strip().lower()
        message = _bounded_message(constraints.get("message"),
                                   constraints.get("max_message_chars"))

        repo = await self._repo_root(workspace)
        if repo is None:
            return AgentResult(
                ok=False, summary=f"not a git repository: {workspace}",
                evidence=[{"kind": "git-operate", "operation": "detect", "repo": False}])

        if operation == "commit" or (not operation and message):
            state_dir = str((request.context.metadata or {}).get("state_dir") or "")
            return await self._commit(repo, message, constraints, state_dir)
        return await self._status(repo, read_only=operation in ("", "status"))

    # ------------------------------------------------------------------ operations

    async def _status(self, repo: Path, *, read_only: bool) -> AgentResult:
        porcelain = await self._run(repo, "status", "--porcelain")
        if porcelain is None:
            return AgentResult(ok=False, summary="git status failed",
                               evidence=[{"kind": "git-operate", "operation": "status"}])
        diff = await self._run(repo, "diff", "--stat")
        changes = [line for line in porcelain.splitlines() if line.strip()]
        head = (await self._run(repo, "rev-parse", "--short", "HEAD")) or ""
        summary = f"git status: {len(changes)} change(s)"
        if head.strip():
            summary += f" at {head.strip()}"
        if changes and read_only:
            summary += " — supply constraints.message to commit"
        return AgentResult(
            ok=True, summary=summary, stdout=porcelain,
            evidence=[{"kind": "git-operate", "operation": "status", "repo": str(repo),
                       "changes": len(changes), "head": head.strip() or None,
                       "diff_stat": (diff or "").strip()[:2000]}])

    async def _commit(self, repo: Path, message: str, constraints: dict,
                      state_dir: str = "") -> AgentResult:
        if not message:
            return AgentResult(
                ok=False,
                summary="commit needs a non-empty constraints.message",
                evidence=[{"kind": "git-operate", "operation": "commit", "error": "message_missing"}])

        paths = constraints.get("paths") or []
        if paths:
            if not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts
                       for p in paths):
                return AgentResult(
                    ok=False,
                    summary="paths must be workspace-relative and must not escape the repository",
                    evidence=[{"kind": "git-operate", "operation": "commit",
                               "error": "invalid_paths"}])
            added = await self._run(repo, "add", "--", *paths)
        else:
            # `git add -A` would stage the fabric's own state directory (.saf by default:
            # evidence, backups, outbox). Never commit our own bookkeeping into the user's repo.
            exclude = _state_exclusion(repo, state_dir)
            if exclude:
                added = await self._run(repo, "add", "-A", "--", ".", f":(exclude){exclude}")
            else:
                added = await self._run(repo, "add", "-A")
        if added is None:
            return AgentResult(ok=False, summary="git add failed",
                               evidence=[{"kind": "git-operate", "operation": "commit",
                                          "error": "add_failed"}])

        staged = await self._run(repo, "diff", "--cached", "--name-only")
        if not (staged or "").strip():
            return AgentResult(
                ok=True, summary="nothing to commit — working tree clean",
                evidence=[{"kind": "git-operate", "operation": "commit", "committed": False,
                           "reason": "clean_worktree"}])

        started = time.monotonic()
        # `-m <message>` is a single argv element: the message is data, never syntax
        committed = await self._run(repo, "commit", "-m", message)
        duration = time.monotonic() - started
        if committed is None:
            return AgentResult(ok=False, summary="git commit failed",
                               evidence=[{"kind": "git-operate", "operation": "commit",
                                          "error": "commit_failed"}])
        sha = (await self._run(repo, "rev-parse", "--short", "HEAD")) or ""
        files = [line for line in staged.splitlines() if line.strip()]
        return AgentResult(
            ok=True,
            summary=f"committed {len(files)} file(s) as {sha.strip() or '<unknown>'}",
            stdout=committed,
            artifacts=files,
            evidence=[{"kind": "git-operate", "operation": "commit", "committed": True,
                       "sha": sha.strip() or None, "files": files,
                       "message_chars": len(message), "duration_s": round(duration, 4)}])

    # ------------------------------------------------------------------ plumbing

    async def _repo_root(self, workspace: Path) -> Path | None:
        top = await self._run(workspace, "rev-parse", "--show-toplevel")
        if top is None:
            return None
        root = top.strip()
        return Path(root) if root else None

    async def _run(self, cwd: Path, *args: str) -> str | None:
        """Run one bounded git command. Returns stdout, or None on any failure."""
        argv = ["git", *args]
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE),
                timeout=self.timeout_s)
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except (asyncio.TimeoutError, OSError, ValueError):
            return None
        if proc.returncode != 0:
            return None
        return out.decode(errors="replace")[:self.max_output_chars]


def _state_exclusion(repo: Path, state_dir: str) -> str:
    """Repo-relative path of the fabric's state dir, when it lives inside the repository."""
    candidates = [state_dir] if state_dir else []
    candidates.append(str(repo / ".saf"))  # convention when no context is available
    for candidate in candidates:
        if not candidate:
            continue
        try:
            rel = Path(candidate).resolve().relative_to(repo.resolve())
        except (ValueError, OSError):
            continue
        if rel.parts:
            return str(rel)
    return ""


def _bounded_message(raw, raw_limit) -> str:
    if raw is None:
        return ""
    try:
        limit = int(raw_limit) if raw_limit is not None else DEFAULT_MAX_MESSAGE_CHARS
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_MESSAGE_CHARS
    limit = max(1, min(limit, HARD_MAX_MESSAGE_CHARS))
    return str(raw).replace("\x00", "").strip()[:limit]

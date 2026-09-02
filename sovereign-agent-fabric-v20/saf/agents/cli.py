import asyncio
import shutil
from pathlib import Path

from .base import AgentRuntime
from saf.core.contracts import AgentResult


class CliAgentRuntime(AgentRuntime):
    """Subprocess agent runtime with bounded execution.

    Hardened against the NFR failure modes in docs/02-engineering-brief.md:
    - `timeout_s` bounds execution (a hung CLI cannot hang the fabric forever);
    - missing binary / missing cwd / spawn errors produce structured results,
      never unhandled exceptions;
    - `argv` is the *fixed* invocation prefix (e.g. ["--message"]) configured by
      the adapter author; the request prompt is passed as a single final
      argument so user-controlled text can never be interpreted as flags.
    """

    binary = ""
    argv: list[str] = []
    timeout_s: float = 600.0

    async def execute(self, request):
        if not self.binary or not shutil.which(self.binary):
            return AgentResult(
                ok=False,
                summary=f"{self.binary or '<unset>'} is not installed or not on PATH.",
            )
        workspace = Path(request.context.workspace)
        if not workspace.is_dir():
            return AgentResult(ok=False, summary=f"workspace does not exist: {workspace}")
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    self.binary, *self.argv, request.prompt,
                    cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.timeout_s,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            return AgentResult(
                ok=False,
                summary=f"{self.binary} timed out after {self.timeout_s}s",
                stderr=f"timeout={self.timeout_s}s",
            )
        except (OSError, ValueError) as exc:
            return AgentResult(ok=False, summary=f"{self.binary} failed to start: {exc}")
        return AgentResult(
            ok=proc.returncode == 0,
            summary=f"{self.binary} exited with {proc.returncode}",
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )


class PiAdapter(CliAgentRuntime):
    binary = "pi"
    resource_id = "agent://pi"
    capability_ids = ["cap://software/agent/execute", "cap://software/code/inspect"]


class CursorCliAdapter(CliAgentRuntime):
    binary = "cursor"
    resource_id = "agent://cursor-cli"
    capability_ids = ["cap://software/agent/execute", "cap://software/code/refactor"]


class CodexCliAdapter(CliAgentRuntime):
    binary = "codex"
    resource_id = "agent://codex-cli"
    capability_ids = ["cap://software/agent/execute", "cap://software/code/refactor"]


class OpenCodeAdapter(CliAgentRuntime):
    binary = "opencode"
    resource_id = "agent://opencode"
    capability_ids = ["cap://software/agent/execute", "cap://software/code/refactor"]


class AiderAdapter(CliAgentRuntime):
    binary = "aider"
    resource_id = "agent://aider"
    capability_ids = ["cap://software/agent/execute", "cap://software/code/refactor"]

from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

class Autonomy(str, Enum):
    A0="A0"; A1="A1"; A2="A2"; A3="A3"; A4="A4"; A5="A5"

class Capability(BaseModel):
    id: str
    version: str = "0.1.0"
    description: str
    tags: list[str] = Field(default_factory=list)
    risk: str = "low"
    implementations: list[str] = Field(default_factory=list)

class Task(BaseModel):
    intent: str
    required_capabilities: list[str] = Field(default_factory=list)
    autonomy: Autonomy = Autonomy.A2
    constraints: dict[str, Any] = Field(default_factory=dict)

class ExecutionContext(BaseModel):
    task_id: str
    workspace: str = "."
    platform: str = "unknown"
    network_available: bool = True
    budget_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class AgentRequest(BaseModel):
    task: Task
    context: ExecutionContext
    prompt: str

class AgentResult(BaseModel):
    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)

class ModelRequest(BaseModel):
    prompt: str
    system: str = ""
    model: str | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class ModelResponse(BaseModel):
    ok: bool
    text: str
    model: str
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

class Candidate(BaseModel):
    resource_id: str
    kind: str
    capabilities: list[str] = Field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.0
    cost_estimate: float = 0.0
    latency_estimate_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    """One capability in the §16 lifecycle: PLAN -> EXECUTE -> VALIDATE -> EVIDENCE."""

    capability: str
    status: str                      # succeeded | unsupported | unavailable | failed | blocked
    resource_id: str | None = None
    summary: str = ""
    duration_s: float = 0.0
    attempts: list[dict[str, Any]] = Field(default_factory=list)   # candidate fall-through trace
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    verified: bool | None = None     # save-proof verdict for mutating steps


class ExecutionResult(BaseModel):
    """Outcome of a full task execution (deterministic record, JSON-safe)."""

    execution_id: str
    intent: str
    ok: bool
    status: str                      # succeeded | partial | failed | blocked
    policy: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepResult] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    rollback_point: str | None = None
    evidence_hash: str | None = None
    workspace: str = "."
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0

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

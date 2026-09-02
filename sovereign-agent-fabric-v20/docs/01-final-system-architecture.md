# SAF V20 — Final System Architecture & End-to-End Design

## Executive architecture decision

Build the Sovereign Agent Fabric (SAF) as the provider-neutral execution and cognition fabric under the PCOS/V20 system.

**Do not begin with a heavyweight gRPC mesh. Do not build a conventional enterprise data mesh.**

Build:
1. capability contracts;
2. agent runtime mesh;
3. model/provider mesh;
4. tool/MCP mesh;
5. VIA-X interaction fabric;
6. cognitive data fabric;
7. verification/evidence fabric;
8. evolutionary transport fabric.

The attached PCOS baseline states that the system is a Personal Cognitive Operating System rather than a collection of scripts/repositories, and establishes the law: capabilities are permanent abstractions while implementations are temporary resources. It also requires discovery, evaluation, ranking, sandboxing, execution and validation while keeping local state bounded. [PCOS source: lines 17–37, 96–120]

## 1. End-to-end topology

```text
HUMAN
 ↓
INTENT INTERFACE
 ↓
CONTEXT + MEMORY
 ↓
INTENT COMPILER
 ↓
CAPABILITY RESOLVER
 ↓
DISCOVERY / EVIDENCE / RANKING
 ↓
TRUST / POLICY
 ↓
RESOURCE ECONOMICS
 ↓
AGENT + MODEL ROUTING
 ↓
EXECUTION GRAPH
 ↓
VIA-X / MCP / CLI / API
 ↓
OBSERVE
 ↓
VALIDATE
 ↓
SAVE
 ↓
PROVE
 ↓
MEMORY + WORLD MODEL
 ↓
LEARN / RERANK
```

The PCOS source explicitly defines the intent → context/memory → intent compiler → capability resolver → discovery/evidence/ranking → trust/policy → resource economy → execution router → runtime → validation → learning/memory loop. [PCOS source: lines 38–66]

## 2. Seven architectural planes

### Plane 0 — Constitutional
Sovereign OS, policies, permissions, autonomy, budget, quality and long-term goals.

### Plane 1 — Intent
Text, voice, CLI, browser and event inputs.

### Plane 2 — Intelligence
Planner, strategist, architect, researcher, coder, reviewer, tester, security and optimizer agents.

### Plane 3 — Agent Runtime Mesh
**Pi CLI, Cursor CLI, Codex CLI, OpenCode, Aider**, plus future runtimes.

### Plane 4 — Model Mesh
**Kimi K3, OpenRouter, Abacus.AI**, plus future models/providers.

### Plane 5 — Tool/Interaction Mesh
MCP, WebMCP, LSP, Git, filesystem, terminal, browser and OS adapters.

### Plane 6 — Cognitive Data + Verification Fabric
Memory, world model, knowledge graph, artifacts, evidence, provenance and telemetry.

Transport cuts across all planes:
**in-process → stdio/IPC → HTTP → gRPC**.

## 3. Agent Runtime Mesh

Agent runtimes are execution resources, not architectural identities.

```text
AgentRuntime
├── PiAdapter
├── CursorCliAdapter
├── CodexCliAdapter
├── OpenCodeAdapter
└── AiderAdapter
```

Common contract:

```text
identity()
capabilities()
health()
execute()
status()
cancel()
artifacts()
events()
```

Not every runtime must implement every operation. Capability declarations must be explicit.

## 4. Model Mesh

```text
ModelProvider
├── KimiK3Adapter
├── OpenRouterAdapter
└── AbacusAIAdapter
```

The model identity is independent from deployment:

```text
Kimi K3
├── direct API
├── OpenRouter
├── local inference
├── vLLM
└── future backend
```

The source requires capability identity to remain implementation-independent. [PCOS source: lines 122–132]

## 5. OpenRouter

OpenRouter is a provider aggregation resource below SAF's own capability-aware router.

SAF decides:
- task capability;
- quality threshold;
- cost ceiling;
- latency ceiling;
- trust requirements;
- context requirements.

OpenRouter is then one route rather than the system's canonical model identity.

## 6. Abacus.AI

Treat Abacus.AI as a broader service adapter:
- model/reasoning capability;
- agent workflow capability;
- MCP/tool capability;
- future specialized services.

Provider-specific APIs remain behind the adapter.

## 7. Kimi K3

Treat Kimi K3 as a first-class model resource with replaceable deployment backends. This supports the architecture's resource abstraction and allows local/open-weight inference to become a future cost and resilience path.

## 8. Capability identity

Canonical namespace:

```text
cap://domain/subdomain/action
```

Examples:

```text
cap://software/code/refactor
cap://software/testing/execute
cap://software/git/operate
cap://interaction/browser
cap://model/reasoning
cap://agent/workflow
cap://memory/retrieve
cap://verification/save-proof
```

A capability record contains identity, version, inputs, outputs, implementations, compatibility, evidence, trust, benchmarks, cost, latency, bandwidth and historical success. [PCOS source: lines 122–132]

## 9. Resolver and ranking

```text
intent
 ↓
capabilities
 ↓
candidate discovery
 ↓
compatibility
 ↓
trust/policy
 ↓
economics
 ↓
ranking
 ↓
execution plan
```

Ranking considers task fit, reliability, maintenance, benchmark performance, security, compatibility, integration, cost, bandwidth, latency, adoption, recency, personal historical success, environment fit and evidence confidence. Score and confidence remain separate. [PCOS source: lines 216–245]

## 10. Intent compiler

Example:

```text
"Refactor authentication, run tests, repair failures and commit."
```

becomes:

```text
inspect
→ understand
→ plan
→ modify
→ test
→ diagnose
→ repair
→ retest
→ review diff
→ commit
→ verify
```

Each node resolves independently.

## 11. Agent-to-agent mesh

MCP is not the same as agent-to-agent communication.

```text
MCP:
Agent → Tool

Agent mesh:
Agent → Agent

Model mesh:
Agent → Model

Data fabric:
Agent → Memory/World Model
```

Use a normalized message envelope:

```json
{
  "message_id": "uuid",
  "task_id": "uuid",
  "sender": "agent://cursor-cli",
  "receiver": "agent://reviewer",
  "type": "review.request",
  "capability": "cap://software/code/review",
  "payload_ref": "cas://...",
  "provenance": {}
}
```

## 12. MCP and VIA-X

The PCOS baseline explicitly positions MCP as an interoperability layer rather than the operating system. The adapter can target MCP, HTTP, CLI, Python or containers. [PCOS source: lines 795–810]

VIA-X is the interaction boundary:

```text
SAF
 ↓
VIA-X
 ├── browser
 ├── macOS
 ├── Windows
 ├── terminal
 ├── voice
 └── desktop
```

Recommended browser priority:
1. structured WebMCP;
2. semantic DOM/accessibility;
3. deterministic browser automation;
4. visual fallback.

## 13. Cognitive Data Fabric

This system needs cognitive state rather than a conventional organizational data mesh.

```text
raw sources
 ↓
structured memory
 ↓
semantic memory
 ↓
relationship graph
 ↓
retrieval
 ↓
task-specific context
```

Memory classes:
- working;
- episodic;
- semantic;
- procedural;
- constitutional;
- world-model/relationship memory.

Durable memory retains provenance, confidence, importance, freshness, dependencies, verification and supersession. Important knowledge is never silently overwritten. [PCOS source: lines 284–428]

## 14. World model

Core entities:

```text
User
Project
Repository
Agent
Model
Capability
Tool
Task
Artifact
Environment
Decision
Dependency
Risk
Outcome
```

Core relationships:

```text
USES
DEPENDS_ON
IMPLEMENTS
EXECUTED_BY
VERIFIED_BY
SUPERSEDES
PRODUCED
CONFLICTS_WITH
```

## 15. Storage lifecycle

```text
HOT active
WARM cached
COLD metadata/retrieval
REMOTE executed elsewhere
```

Content-addressable artifacts:

```text
artifact → content hash → CAS://hash
```

The source requires deduplication and explicitly rejects installing every discovered capability locally. [PCOS source: lines 267–282]

## 16. Verification Fabric

Canonical lifecycle:

```text
TASK
→ PLAN
→ EXECUTE
→ RESULT
→ VALIDATE
→ EVIDENCE
→ RANKING UPDATE
→ MEMORY UPDATE
```

[PCOS source: lines 879–896]

For mutations:

```text
snapshot before
→ execute
→ snapshot after
→ compare hashes/state
→ verify expected condition
→ append evidence
→ return verified result
```

Agent claims are not evidence.

## 17. Security

Trust pipeline:

```text
DISCOVERED
 ↓
UNTRUSTED
 ↓
STATIC ANALYSIS
 ↓
DEPENDENCY + LICENSE CHECK
 ↓
SANDBOX
 ↓
FUNCTIONAL + RESOURCE + SECURITY TEST
 ↓
VERIFIED
 ↓
TRUSTED
```

Permissions include filesystem, network, credentials, shell, privilege, financial actions and human approval. [PCOS source: lines 246–265]

## 18. Autonomy

```text
A0 observe
A1 analyze
A2 plan
A3 bounded execution
A4 autonomous execution
A5 adaptive orchestration
```

Recommended initial policy:
- read/research: A4/A5;
- planning: A5;
- code writes: A3;
- external side effects: A3;
- destructive/financial: explicit approval.

## 19. Resource economics

Track:
- input/output tokens;
- cached/retrieved tokens;
- reasoning;
- embeddings;
- compute;
- storage;
- bandwidth;
- latency;
- money;
- human attention;
- future reuse value.

The source's objective is maximum useful work per unit of token, money, compute, bandwidth, storage, energy and human attention. [PCOS source: lines 430–473]

## 20. Semantic compression

```text
deduplication
→ references
→ stable serialization
→ prefix caching
→ semantic caching
→ retrieval scoping
→ pruning
→ compression
→ model routing
→ reasoning-budget control
→ deterministic computation
```

Compression must preserve meaning, conditionals, negations, provenance and safety constraints. [PCOS source: lines 918–935; 1182–1199]

## 21. Offline/disconnected mode

```text
LOCAL STATE
 ↓
CHANGE QUEUE
 ↓
OUTBOX
 ↓
offline → persist
online → sync → acknowledge → reconcile
```

Operations carry IDs, device IDs, timestamps, payload hashes, dependencies and status. The source explicitly requires weak-network tolerance, resumability, checkpointing, deduplication, graceful degradation and local-model fallback. [PCOS source: lines 764–793]

## 22. gRPC decision

### V0/V1
No mandatory gRPC.

Use:
```text
in-process
stdio
HTTP
MCP
subprocess
```

### V2
Introduce gRPC for:
- remote workers;
- GPU execution;
- service isolation;
- high-concurrency orchestration;
- streaming;
- multi-machine scheduling.

The gRPC schema must be derived from domain contracts.

## 23. Failure engineering

Assume:
- process crash;
- model timeout;
- provider outage;
- partial file write;
- network loss;
- stale memory;
- malformed tool output;
- duplicate event;
- conflicting agent recommendations;
- permission denial.

Controls:
- idempotency keys;
- bounded retries;
- circuit breakers;
- checkpointing;
- fallback routing;
- durable failure evidence;
- resumability;
- contradiction escalation.

## 24. Contradiction engine

```text
new decision
 ↓
prior decisions
 ↓
constitution
 ↓
current evidence
 ↓
contradiction?
 ├─ no → proceed
 ├─ low risk → challenge/log
 └─ material → human escalation
```

The source defines the goal as evidence-based correction rather than blind obedience/opposition. [PCOS source: lines 976–993]

## 25. Observability

Every task should carry:
- task ID;
- execution ID;
- correlation ID;
- agent ID;
- model ID;
- capability ID.

Metrics:
- verified completion rate;
- save-proof failure rate;
- task success;
- model quality;
- agent quality;
- cost per successful task;
- latency;
- fallback rate;
- memory utility;
- contradiction rate.

## 26. Repository architecture

```text
sovereign-agent-fabric/
├── saf/
│ ├── core/
│ ├── agents/
│ ├── models/
│ ├── tools/
│ ├── memory/
│ ├── evidence/
│ ├── transport/
│ ├── economy/
│ ├── runtime/
│ └── cli/
├── config/
├── docs/
├── tests/
└── scripts/
```

This is an executable refactoring of the source baseline's broader module structure. [PCOS source: lines 898–905; 1313–1364]

## 27. Engineering standards

- Python 3.11+.
- Pydantic contracts.
- Async I/O boundaries.
- Dependency inversion.
- Provider SDK isolation.
- Structured logging.
- Deterministic core functions.
- Unit + contract + integration tests.
- Versioned schemas.
- ADRs for material architecture changes.
- No silent state mutation.

## 28. API boundary

Future service surface:

```text
POST /v1/tasks
GET /v1/tasks/{id}
POST /v1/tasks/{id}/cancel
GET /v1/capabilities
GET /v1/agents
GET /v1/models
GET /v1/executions/{id}
GET /v1/evidence/{id}
POST /v1/memory/search
```

The same domain contracts should feed future gRPC definitions.

## 29. Two-hour vertical slice

0–15: contracts, registry, CLI
15–35: intent compiler/resolver
35–55: Pi/Cursor/Codex/OpenCode/Aider adapters
55–75: policy/evidence/save-proof
75–90: Kimi/OpenRouter/Abacus boundaries
90–105: memory/world-model foundation
105–120: real task + tests + evidence

The two-hour objective is a genuinely executable, auditable vertical slice—not pretending the entire V20 platform is finished.

## 30. Definition of done

A feature is complete only when:
1. saved;
2. executable;
3. tested;
4. observed;
5. independently verified;
6. evidence recorded;
7. policy satisfied;
8. rollback/recovery understood.

## 31. V20 evolution

V0 — personal control plane
V1 — capability mesh
V2 — autonomous capability fabric
V3 — 100,000× capability universe
V20 — sovereign, constitutional, adaptive, distributed cognitive execution fabric

The attached baseline defines V0–V3 around these same progression points. [PCOS source: lines 533–550]

## 32. Final architectural principle

Do not ask:

> Which agent are we building around?

Ask:

> Which capability must be satisfied, under which constraints, with what evidence, and which available implementation is currently the best verified resource?

That is the center of gravity of SAF V20.

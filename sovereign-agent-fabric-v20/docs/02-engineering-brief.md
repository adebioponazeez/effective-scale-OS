# SAF V20 — L12 Principal Architect Engineering Brief

## Mission
Create a provider-neutral, capability-first operating fabric in which Pi CLI, Cursor CLI, Codex CLI, OpenCode, Aider, Kimi K3, OpenRouter, Abacus.AI, MCP and VIA-X can cooperate without any single provider becoming the system's architectural dependency.

## Architectural decisions
1. Capability identity is canonical.
2. Agent runtimes are replaceable.
3. Models/providers are replaceable.
4. MCP is a tool boundary.
5. Cognitive Data Fabric replaces premature data-mesh complexity.
6. gRPC is evolutionary.
7. Verification is independent from agent claims.
8. Durable state outlives agents.
9. Offline operation is first-class.
10. Resource economics is part of scheduling.

## NFRs
### Reliability
Idempotency, checkpoints, bounded retries, circuit breakers, fallbacks.

### Security
Least privilege, sandboxing, credential isolation, policy-before-execution, explicit approval.

### Performance
Parallel task graph nodes, caching, retrieval scoping, deterministic computation, model routing.

### Cost
Token accounting, model/provider routing, local fallback, semantic reuse accounting.

### Portability
macOS/Windows primary user platforms; server/cloud/Linux execution adapters where useful.

### Maintainability
Typed contracts, isolated adapters, conformance tests, ADRs, versioned schemas.

## Risk register

| Risk | Severity | Control |
|---|---|---|
| Provider lock-in | High | adapter contracts |
| Agent runtime drift | High | capability discovery + conformance tests |
| Prompt/tool injection | Critical | sandbox + least privilege + trust gates |
| False completion | Critical | save-proof + evidence ledger |
| Premature distribution | High | local-first transport |
| Memory corruption | High | provenance/versioning/supersession |
| Cost explosion | High | routing/economics |
| Context bloat | High | retrieval/compression |
| Stale metadata | Medium | health/evaluation refresh |
| Offline divergence | Medium | operation IDs + reconciliation |

## Definition of done
Code existing is insufficient. The implementation must be saved, tested, observed, independently verified and represented in durable evidence.

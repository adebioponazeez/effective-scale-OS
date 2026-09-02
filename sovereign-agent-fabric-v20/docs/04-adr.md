# Architecture Decision Records

## ADR-001 — Capability-first identity
Capabilities, not vendors or repositories, are canonical.

## ADR-002 — No mandatory gRPC at bootstrap
Start local-first. Add gRPC when remote workers, GPU scheduling, streaming or service isolation justify it.

## ADR-003 — Cognitive Data Fabric
Use memory/world-model/evidence architecture instead of premature conventional data-mesh infrastructure.

## ADR-004 — MCP is not the OS
MCP is one interoperability boundary among CLI, Python, HTTP and containers.

## ADR-005 — Separate agent and model meshes
Agent runtimes and models have separate contracts and registries.

## ADR-006 — Evidence before trust
Runtime claims are not proof. Verification inspects actual state.

## ADR-007 — Durable state
Agents may be disposable; OS state, provenance and evidence must survive runtime retirement.

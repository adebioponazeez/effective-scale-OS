# Sovereign Agent Fabric (SAF) V20

Principal-architect reference implementation for the PCOS/V20 architecture.

## Core laws
- Capabilities are permanent abstractions; implementations are temporary resources.
- Agents are disposable; durable state is not.
- MCP is an interoperability boundary, not the OS.
- gRPC is an optional distributed transport, not the kernel.
- Agent runtimes and model providers are independently replaceable.
- Score and confidence are separate.
- Material mutations require independent verification and save-proof.
- Deterministic computation replaces unnecessary LLM calls.
- Local operational state remains bounded as the capability universe grows.

## Initial runtime mesh
Pi CLI, Cursor CLI, Codex CLI, OpenCode, Aider.

## Initial model/service mesh
Kimi K3, OpenRouter, Abacus.AI.

## Tool boundary
MCP, filesystem, Git, terminal; future WebMCP/VIA-X adapters.

## Run
```bash
python -m saf.cli.main doctor
python -m saf.cli.main capabilities
python -m saf.cli.main run "refactor repository and run tests"
pytest -q
```

## Hardening (docs/05-review-and-hardening.md)
- Durable locked JSONL (`fsync`) for memory and evidence; torn writes are
  quarantined to `*.corrupt`, never fatal.
- Hash-chained evidence ledger with `verify()` (tamper detection).
- Bounded agent subprocesses (timeout), fixed argv prefix (no flag injection),
  structured failure results.
- Async-correct, failure-mapped model adapters; word-boundary intent/policy.

## Integrate with effective-scale-OS (optional)
SAF submits the capability plan as an idempotent, observable workflow in the
workload kernel (one event node per required capability). The kernel records,
schedules and observes; SAF owns intent, policy, capability ranking and evidence.

```bash
PYTHONPATH=../src python3 -m effective_scale --store /tmp/es.db \
    --listen 127.0.0.1:8080 --admin-token dev --demo &
saf run --es http://127.0.0.1:8080 \
    --token "$TOKEN" --namespace default "refactor repository and run tests"
```

If the kernel is unreachable SAF degrades to local mode with a structured
error (offline operation is first-class, docs/01 §21).

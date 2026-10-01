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
python -m saf.cli.main run "refactor repository and run tests"     # plan (optional --es to record)

# execute the plan with the §16 lifecycle (deterministic capabilities run with no model):
python -m saf.cli.main execute "inspect repository and run tests" --workspace . --state-dir .saf
python -m saf.cli.main verify --state-dir .saf                     # evidence hash chain
python -m saf.cli.main rollback latest --workspace . --dry-run     # snapshot -> restore
python -m saf.cli.main resources --stats
pytest -q                                                           # 77 tests
```

## Hardening (docs/05-review-and-hardening.md §6)
- Durable locked JSONL (`fsync`) for memory and evidence; torn writes are
  quarantined to `*.corrupt`, never fatal.
- Hash-chained evidence ledger with `verify()` (tamper detection).
- Bounded agent subprocesses (timeout), fixed argv prefix (no flag injection),
  structured failure results.
- Async-correct, failure-mapped model adapters; word-boundary intent/policy.
- **Execution** (`runtime/executor.py`): PLAN→EXECUTE→VALIDATE→EVIDENCE→MEMORY with
  candidate fall-through; mutating steps are save-proofed from disk, never from claims.
- **Deterministic local runtime** (`saf://local`): inspect / run tests / save-proof
  with no model call, bounded and unsupported-by-default for everything else.
- **Rollback** (`tools/backup.py`): content-addressed points, hash-verified restore,
  explicit `unrestorable` reporting (never silent data loss).
- **Offline outbox** (`transport/outbox.py`, docs §21): durable entries with payload
  hashes; `saf sync` reconciles via the kernel idempotency key.
- **Kernel worker** (`runtime/worker.py`): lease-bound claim → execute → heartbeat →
  complete, under the kernel's deadline, fenced by nonce, offline-safe.

## Integrate with effective-scale-OS (optional, now executes)

Two modes, both idempotent:

1. **Plan record** — `saf run --es …` submits one event node per capability; the
   kernel records and observes the plan.
2. **Real execution** — submit with a `workload_id` and run `saf worker`: the kernel
   schedules lease-bound attempts, SAF claims them (fencing nonce), executes each
   capability through the §16 lifecycle, heartbeats, and completes with the
   evidence pointer. Capabilities are chained in declared order.

```bash
PYTHONPATH=../src python3 -m effective_scale --store /tmp/es.db \
    --listen 127.0.0.1:8080 --admin-token dev --demo &
TOKEN=$(curl -fsS -XPOST http://127.0.0.1:8080/v1/tokens -H 'X-Admin-Token: dev' \
    -d '{"namespace":"default","scopes":["read","write"]}' \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

saf run    --es http://127.0.0.1:8080 --token "$TOKEN" "refactor repository and run tests"
saf worker --es http://127.0.0.1:8080 --token "$TOKEN" --workspace . --max-jobs 4
saf sync   --es http://127.0.0.1:8080 --token "$TOKEN"   # offline outbox reconciliation
```

If the kernel is unreachable, `saf run` persists the submission in the outbox and
`saf sync` replays it idempotently — offline operation is first-class (docs/01 §21).

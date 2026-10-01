# 00 — Overview & Guided Tour

> Reader: engineering leads, architects, auditors. This is the "why" and the "walk-through".
> Companion docs: `01-requirements.md`, `02-architecture.md`, `03-edge-cases-failure-modes.md`,
> `04-tradeoff-matrix.md`, `05-..-ADRs`, `06-security.md`, `07-observability-slo.md`,
> `08-scaling-playbook.md`, `09-api-reference.md`, `10-go-porting-blueprint.md`.

## 1. Problem

Running workloads at "effective scale" usually means stitching together: a scheduler, a job runner, an
event bus, an autoscaler, auth, metrics, and retry logic — each with its own failure modes, security
model and operational rituals. The result is brittle: idempotency is handled ad hoc, retries double-run
work, autoscalers flap, backpressure disappears under load, and there is no single audit trail.

`effective-scale-OS` is the **single kernel** for those four planes, built around a small number of
invariant-safety primitives (leases, idempotency keys, state machines, backpressure) so that the
"impossible" cases (crash between retry and commit, duplicate events, cancelled-but-still-running
work, scale-down during a spike) are handled by the platform instead of by each workload author.

## 2. Mental model

```
                 ┌──────────────────────────────────────────────┐
   clients ───►  │  API CONTROL PLANE  (auth, rl, idempotency)  │
                 └──────────────┬───────────────────────────────┘
                                │ commands
        ┌───────────────────────┼────────────────────────────────────┐
        │                       ▼                                    │
        │  ┌──────────────────────────────┐   ┌───────────────────┐  │
        │  │ WORKFLOW PLANE               │   │ COMPUTE PLANE     │  │
        │  │ DAG engines, retries,        │   │ schedulers,       │  │
        │  │ timeouts, cron, leases       │   │ autoscaling,      │  │
        │  └──────────────┬───────────────┘   │ placement         │  │
        │                 │                 └─────────┬─────────┘  │
        │                 │  dispatch                  │ scale      │
        │                 ▼                            ▼            │
        │  ┌──────────────────────────────┐   ┌───────────────────┐  │
        │  │ DATA/EVENT PLANE             │◄──┤ WORKERS / NODES   │  │
        │  │ topics, partitions,          │   │ (registered exec) │  │
        │  │ consumer groups, DLQ         │   └───────────────────┘  │
        │  └──────────────┬───────────────┘                          │
        │                 ▼                                          │
        │  OBSERVABILITY (metrics, logs, traces, audit) + RESILIENCE │
        └────────────────────────────────────────────────────────────┘
```

Everything is a **record with a lifecycle state machine**. Nothing moves unless it is persisted
first (write-ahead), and nothing is re-applied unless the idempotency key matches. That one rule is
what makes retries safe.

## 3. Guided tour (5 minutes)

1. **Start the kernel** — `make run`. It binds `:8080`, opens the store (SQLite WAL by default),
   elects a leader (itself), and starts watchdog + scaler loops.
2. **Register a namespace** — multi-tenant isolation lives on every record.
3. **Define a workload** — `POST /v1/workloads` with desired replicas, resource
   `cpu`/`memory`, and a `policy`. The scheduler now owns placement.
4. **Ask for work** — a worker calls `POST /v1/workloads/{id}/lease` (or subscribes).
   Leases are time-boxed and renewable; a crashed worker's lease expires and the slot is re-dispatched.
5. **Submit a workflow** — `POST /v1/workflows` with a DAG; each node is a
   `task` referencing a workload or an event. The engine executes in dependency order with
   retry/backoff, timeouts and a durable state transition per node.
6. **Publish events** — `POST /v1/events` partitions by key, snapshots progress, and dead-letters
   poison messages after max attempts.
7. **Watch the numbers** — `GET /v1/metrics` (counters/gauges/histograms), `GET /v1/health/ready`,
   `GET /v1/audit` for the trail. Structured logs on stdout; trace ID keyed end-to-end.
8. **Kill the process mid-work** — on restart the store replays state; in-flight leases expire and
   work is re-queued exactly once from the platform's perspective.

## 4. What this is NOT

- Not a container runtime — it schedules *workloads* (exec units it can lease to workers); a worker
  adapter can map a lease to a process, a container, a cloud function or a queue message.
- Not a replacement for a relational DB at multi-node scale — the default store is SQLite (single
  writer); the store port is narrow and Postgres is the documented production adapter (ADR-004).
- Not a distributed consensus system — leadership is lease-based, best-effort, single-writer
  (ADR-003); correctness under split-brain is bounded by lease TTL, never by "hope".

## 5. Version & compatibility

- API is versioned (`/v1`). Breaking changes bump the prefix.
- State format is versioned inside the store; migrations run on open and refuse to downgrade.
- Semantic versioning for the package, declared once in `effective_scale.__version__` and enforced
  by the `C-versions` audit check (kernel 0.5.0, SAF 0.2.0 as of this revision).
- CI gates: unit + concurrency + chaos + audit (ontology/docs drift) + SAF suite.

## 6. How this package is kept honest

Claims in this documentation set are enforced, not asserted. The system model lives in
[`ontology/system.json`](../ontology/system.json) — planes, entities, state machines, invariants
K1–K20 (each naming its enforcement point and its test), declared capabilities and product versions.
`python3 tools/audit.py` (also `make audit`) compares that ontology with the code and the docs in nine
checks, with stable finding ids and a machine-readable `--json` mode. Gaps that are knowingly accepted
are listed in [`ontology/known-gaps.json`](../ontology/known-gaps.json) and **that list may only
shrink** — `tests/test_audit.py` fails the build if a fixed gap is left in it, and it injects synthetic
drift to prove every check can actually fail. The human-readable rendering of the ontology is
generated (`make ontology` → [`11-ontology.md`](11-ontology.md)); never edit it by hand. The
evidence-anchored audit of the current state, and the reproduction commands for every number in it,
are in [`GAP-AUDIT.md`](../GAP-AUDIT.md).

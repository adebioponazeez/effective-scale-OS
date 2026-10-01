# 08 — Scaling Playbook (single node → cluster)

The kernel is built to be **scale-out honest**: single-node is a deliberate, tested state; the
path to multi-node is a sequence of adapter swaps, not a rewrite. This playbook tells you exactly
when and how.

## 1. Read the signal before you scale

| Signal | What it means | Action |
|---|---|---|
| `workflow.node_latency` p95 rising | executor saturated | add workers / workload replicas first — the platform is not the bottleneck |
| `eventbus.pending` near max_lag | consumers slow | scale consumer group workers; then raise `--event-max-lag` with caution |
| `scheduler.leases.granted` churn | placement pressure | add nodes (or pool tags) before touching the kernel |
| `loop.*.last_ts` gap | kernel stall | watchdog already crashes it; investigate, don't scale |
| API p95 > 100 ms, CPU > 50 % | kernel saturated | THIS is when the split below starts |

## 2. Stages (each is a config/ops change, not a rewrite)

### Stage 0 — Single process, SQLite file (default)
Target: one node, ≤ ~100k records, ≤ a few thousand ops/s. Operates everything above.

### Stage 1 — Vertical (0 deployment risk)
- Raise `--max-conns` / `--workflow-pool` with CPU headroom.
- Reduce `--scheduler-interval`/`--workflow-interval` only if loop.last_ts shows idle gaps.
- Confirm disk has fsync capacity (WAL + FULL).

### Stage 2 — Split planes by load (ADR-005 trigger: any plane > 50 % CPU sustained)
Surfaces already exist:
- **API** can run as its own process sharing the store (readers only) + write-through queue to
  the kernel process (leader) — the single-writer contract holds.
- **Event bus** pump can become its own process: `EventBus` is already a class with no kernel
  coupling; run it against the same store **reads** while publishes go through the leader.
- Scheduler/scaler stay on the leader (they are cheap, deterministic).

#### Worker fleets (the actual compute scaling lever)

Workers are **pull-based and stateless** (ADR-006): each one polls
`GET /v1/attempts?claimable=true`, claims a lease-bound attempt, heartbeats, and completes it.
Consequences for scaling:

- add workers = add replicas; no kernel change, no rebalancing protocol, no worker registry;
- the claim is the load balancer: whichever worker polls first owns the attempt, and the lease
  TTL bounds how long a dead worker's slot is held;
- scale-down is safe: stop a worker and its in-flight attempt is either finished before the
  lease lapses or re-dispatched by retry policy (`lease_expired`);
- capacity is bounded by workload replicas (slots), not by worker count — adding workers past
  the slot count only increases poll traffic, which is why polling intervals are configurable.

### Stage 3 — Store HA (ADR-002/004 trigger: downtime needs, files > 10 GB)
- Swap `SQLiteStore` → `PostgresStore` (same 12-method port; the SQL is already table-shaped).
- Leadership: Postgres advisory lock or the lease row (already in `meta`).
- Rolling restart becomes safe: old leader's writes are epoch-rejected (tested).

### Stage 4 — Multi-node by design (only if measured)
- Workers are external (they already lease/report via API).
- Consumers are external (they already fetch/ack via API).
- The kernel remains single-writer per store; multiple kernels = sharded namespaces.

## 3. What we deliberately did NOT build (and why)

- Embedded Raft/consensus in v1 — a lease + epoch scoping covers the realistic failure (process
  restart) with provable bounds; consensus adds invariants and a dependency, not correctness
  for single-writer workloads.
- Exactly-once end-to-end — impossible without external transaction coordination (ADR-003).
- ML placement — non-determinism taxes auditability for a v1 (D6 trade-off matrix).

## 4. Cost discipline

- One process ≈ 30–60 MB RSS with 100k records (snapshot-cache dominated); no per-message
  queues (backpressure rejects instead).
- Zero third-party runtime dependencies: the container is the standard library + our code.

## 5. Capacity math (approx, local store)

- Reads: served from memory — O(1) per record; 10k+ reads/s per core on a laptop.
- Writes: single writer, ~5–20 µs/op in WAL; bursts queue (observable via `writes` counter).
- Workflow dispatch scan: O(nodes) per tick — batch large DAGs (`max_concurrency` bounds
  dispatch width; 1k-node DAGs measured OK at 500 ms tick).

## 6. Operating it (supervision is part of the scaling story)

Scaling a fleet you cannot keep alive is decoration. The kernel is single-writer and
WAL-backed: `tests/test_chaos.py` proves an abrupt close replays without double-commit, and
`tests/test_soak.py` proves sustained load keeps every loop alive with bounded state and no
duplicate attempts after an abrupt restart. So the operating policy is **restart, do not
nurse** — and every supervisor we ship does exactly that (enforced by the `C-ops` audit check):

| Runtime | Artifact | Policy |
|---|---|---|
| Linux | `deploy/systemd/effective-scale.service` | `Restart=always`, `RestartSec=2`, 35s SIGTERM drain, hardening, `ReadWritePaths=/var/lib/effective-scale` |
| Windows | `deploy/windows/install-service.ps1` | SCM automatic start + `sc.exe failure` restart-2s/2s/5s |
| Kubernetes | `deploy/k8s/03-deployment.yaml` | `startupProbe` (live), `readinessProbe` (ready), `livenessProbe` (live), 30s grace, `replicas: 1` |
| Compose | `docker-compose.yml` | healthcheck on `/v1/health/ready`, `restart: unless-stopped` |
| Image | `Dockerfile` | `HEALTHCHECK` against `/v1/health/ready` |

Probe semantics matter when a probe drives traffic:

- `GET /v1/health/live` — liveness: the process is up and its loops are alive; it never touches
  the store, so a recoverable store failure does not get the process killed.
- `GET /v1/health/ready` — readiness: the kernel can *commit* — it asks the writer thread to run
  the store's own probe (`SELECT 1` on the kernel's connection). A dead store or a wedged writer
  returns 503 while `live` stays 200. Asserted in `tests/test_cli.py::HealthSemanticsTest`.

Before you scale out, run the longevity check on your own hardware:

```bash
make soak     # sustained load: terminal workflows, bounded attempts/leases, no loop errors
make audit    # every deployment artifact still declares probes + a restart policy (C-ops)
```

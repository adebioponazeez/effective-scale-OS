# 01 — Requirements Specification

Reference: ISO/IEC/IEEE 29148-style. "MUST" is normative; "SHOULD" is a strong default; "MAY" is optional.
Every requirement maps to a test in `tests/`.

## 1. Functional requirements

### FR-1 Control plane
| ID | Requirement |
|---|---|
| FR-1.1 | The platform SHALL expose an HTTP/1.1 REST API under `/v1`. |
| FR-1.2 | Clients SHALL authenticate with a scoped API token (HMAC-signed, verifiable offline) or a JWT. |
| FR-1.3 | The API SHALL reject unknown routes, malformed JSON (400), unauthorized calls (401), forbidden scopes (403), invalid token (401) — never 500 for client errors. |
| FR-1.4 | Idempotency: POSTs carrying `Idempotency-Key` SHALL be applied at most once; a repeated key returns the original response (replay-safe, 90-day retention). |
| FR-1.5 | Multi-tenancy: every resource SHALL be scoped to a namespace; cross-namespace access SHALL fail. |
| FR-1.6 | Rate limiting SHALL be per-namespace/per-token with token bucket semantics and correct `Retry-After`. |

### FR-2 Compute plane
| ID | Requirement |
|---|---|
| FR-2.1 | Workloads SHALL declare: replicas, resources (cpu, memory), image, placement policy, health command. |
| FR-2.2 | Scheduler SHALL support: FIFO, priority, round-robin, bin-packing (first-fit-decreasing by demand). |
| FR-2.3 | Leases SHALL be time-boxed, renewable, and re-dispatchable at expiry (crash safety). |
| FR-2.4 | Placement SHALL respect: capacity, node tags/constraints, spread policy, anti-affinity, pinned nodes. |
| FR-2.5 | Nodes SHALL be cordonable/drainable; draining migrates leases without violating capacity. |
| FR-2.6 | Auto-scaler SHALL scale replicas from metrics (CPU, memory, queue depth, throughput) with hysteresis, cooldown, min/max bounds and scale-down protection. |
| FR-2.7 | Scaling decisions SHALL be persisted and recorded in the audit log. |

### FR-3 Workflow plane
| ID | Requirement |
|---|---|
| FR-3.1 | Workflows SHALL be DAGs; workflows SHALL be executable in dependency order with fan-in/fan-out. |
| FR-3.2 | Node execution SHALL be idempotent (attempt deduplication by node+attempt id). |
| FR-3.3 | Retries SHALL follow exponential backoff with jitter, bounded by max attempts. |
| FR-3.4 | Per-attempt timeout SHALL abort the attempt; a global workflow timeout SHALL cancel the workflow. |
| FR-3.5 | Cancel SHALL propagate: pending nodes skipped, running nodes signalled cancelled, outcome recorded. |
| FR-3.6 | Cron schedules SHALL trigger workflow submissions with schedule metadata in events. |
| FR-3.7 | Sibling concurrency (branch parallelism) SHALL be bounded by a configured limit. |
| FR-3.8 | State transitions SHALL be write-ahead: a node moves to a new state only after persistence. |
| FR-3.9 | Failed terminal nodes SHALL route to a dead-letter workflow if configured. |

### FR-4 Data/event plane
| ID | Requirement |
|---|---|
| FR-4.1 | Topics SHALL be partitionable by key; ordering SHALL be preserved within a partition. |
| FR-4.2 | Consumers SHALL use consumer groups with checkpoint offsets and resumability after crash. |
| FR-4.3 | Backpressure SHALL surface as observable lag and SHALL NOT cause unbounded memory growth. |
| FR-4.4 | Poison messages (N failed deliveries) SHALL move to the topic's DLQ with the error recorded. |
| FR-4.5 | Events SHALL carry schema version + generated `trace_id`. |

### FR-5 Resilience & operations
| ID | Requirement |
|---|---|
| FR-5.1 | Liveness (`/health/live`) SHALL be independent of dependencies; readiness (`/health/ready`) SHALL fail if the store is unavailable. |
| FR-5.2 | Graceful shutdown SHALL stop accepting, finish in-flight user requests, release leases, and flush the store. |
| FR-5.3 | Crash recovery SHALL re-queue expired leases and incomplete nodes on start. |
| FR-5.4 | Circuit breakers SHALL trip on error-rate/error-count thresholds, half-open probe, and recover automatically. |
| FR-5.5 | Worker pools (bulkheads) SHALL bound concurrency per plane and reject with backpressure above the bound. |
| FR-5.6 | Leader election SHALL use leases with heartbeat; a lost lease SHALL demote cleanly. |
| FR-5.7 | Watchdog SHALL detect stuck loops/dirty shutdown and emit fatal alert + auto-restart signal. |

### FR-6 Observability & security
| ID | Requirement |
|---|---|
| FR-6.1 | Logs SHALL be structured (JSON), with `trace_id`, `event`, `ts`; secrets SHALL be redacted. |
| FR-6.2 | Metrics SHALL expose counters, gauges and histograms (latency buckets, sizes). |
| FR-6.3 | Every mutating call SHALL write an audit entry (actor, action, resource, outcome, ts). |
| FR-6.4 | Secrets in config SHALL be detected and masked; tokens SHALL be hashed at rest. |

## 2. Non-functional requirements (NFR)

| ID | NFR | Target |
|---|---|---|
| NFR-1 | **Throughput** | ≥ 2,000 API ops/s per 1 vCPU (local store, 1KB payloads) |
| NFR-2 | **Latency** | p50 < 5 ms, p95 < 25 ms, p99 < 100 ms for control-plane reads (no load) |
| NFR-3 | **Availability** | ≥ 99.9% per node; no data loss on abrupt kill (fsync'd WAL) |
| NFR-4 | **Recovery** | cold start < 2 s for 100k records |
| NFR-5 | **Elasticity** | scaler reaction ≤ 30 s from metric change to decision |
| NFR-6 | **Determinism** | scheduler is a pure function of (state, policy) — replayable |
| NFR-7 | **Portability** | zero third-party runtime deps; runs on CPython 3.11+, musl/glibc, x86_64/arm64 |
| NFR-8 | **Security** | no plaintext secrets; token hashes at rest; constant-time compare |
| NFR-9 | **Observability** | every hop carries a trace id; audit trail immutable |
| NFR-10 | **Maintainability** | hexagonal: domain has zero imports from adapters/API |
| NFR-11 | **Testability** | clock + store injected; chaos tests kill the process at random points |
| NFR-12 | **Cost** | single small binary/process; ~10 MB RSS idle |

## 3. Key invariants (the "correctness spine")

1. **Write-ahead, then act.** State changes persist before effects (lease grant, message delivery).
2. **At-most-once apply, exactly-once intent.** Idempotency keys make retries safe.
3. **Leases bound ambiguity.** A crashed worker's effect window ends at lease expiry; the platform
   never "guesses" a worker is dead before TTL.
4. **Backpressure beats memory growth.** Every bounded queue has a cap and a rejection signal.
5. **Deterministic core.** Scheduling and state machines take `(snapshot, policy, now)` → decision;
   no hidden randomness except documented jitter.

## 4. Out of scope (v1)
- Multi-region replication, horizontal DB sharding.
- GPU scheduling and latency-aware (NUMA) placement.
- Fancy workflow DSL beyond DAG (loops/conditionals are expressible via tasks + events).
- Standalone UI (the API + `metrics` endpoint is the surface; a UI is an adapter).

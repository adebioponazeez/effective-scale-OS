# 02 — Architecture

## 1. Shape: hexagonal (ports & adapters)

```
            ┌──────────────────────────── HTTP API ────────────────────────────┐
            │  auth · rate-limit · idempotency · schema validation · tracing   │
            └───────────────┬───────────────────────────────────┬──────────────┘
                            │        (commands/queries)         │
   ┌────────────────────────▼───────────────────────────────────▼───────────────┐
   │                            APPLICATION CORE                                │
   │  Scheduler   Scalers   WorkflowEngine   EventBus   Resilience   Leader     │
   │  ──────────────────────────────────────────────────────────────────────────│
   │  DOMAIN (pure): entities, value objects, state machines, policies          │
   └───────┬──────────────────────────┬──────────────────────────┬──────────────┘
           │ ports: Store             │ ports: Clock/Timer       │ ports: Logger/Random
   ┌───────▼──────────────┐   ┌───────▼──────────────┐   ┌───────▼──────────────┐
   │ SQLiteStore (default)│   │ SystemClock          │   │ JSONLogger           │
   │ InMemoryStore (tests)│   │ FakeClock (tests)    │   │ MemLogger (tests)    │
   │ (Postgres per ADR-4) │   │                      │   │                      │
   └──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

**Rule (drives all tests):** `domain/` and `core/` import only `domain`, `ports` and stdlib.
`adapters/` implement `ports`. `api/` adapts HTTP → core calls and is the only place that knows
about HTTP. This is what makes the Go port a mechanical translation (see `10`).

## 2. Process model

One process, one writer:

| Loop role | Purpose | Bound |
|---|---|---|
| Writer | serializes every mutation (single-writer queue; reentrancy-safe) | 1 thread |
| API server | serve HTTP, authenticate, validate, dispatch (reads only) | bulkhead: `max_conn` |
| Scheduler loop | every `schedule_interval` runs pure function → apply decisions | 1 thread |
| Scaler loop | every `scale_interval` reads metrics → hysteresis decisions | 1 thread |
| Workflow pump | scan due nodes → dispatch attempts (bounded worker pool); lease-bound attempts are completed by external workers via the attempts API (ADR-006) | `workflow_pool` |
| Event pump | dispatch checkpointed messages to consumers (bounded pool) | `event_pool` |
| Leader loop | heartbeat lease, watch for demotion | 1 thread |
| Watchdog | monotonic stalls detection; fatal alert + graceful restart | 1 thread |

Readers never write: they take the current `Snapshot` (atomic reference swap after each
commit), so cross-field consistency is guaranteed per snapshot. State races are impossible
by construction, and the suite runs the same interleavings in CI stress + chaos runs.

## 3. Layered interfaces (ports)

```python
class Store(Protocol):          # persistence: CRUD + keyed rows, snapshot, WAL durability
class Clock(Protocol):          # now() monotonic, sleep(), schedule callbacks
class Logger(Protocol):         # log(event, level, fields, trace_id)
class RandomSource(Protocol):   # jitter, lease nonce, tracing ids
class LeaseManager(Protocol):   # acquire/renew/release/observe
class CircuitBreaker(Protocol): # allow()/success()/failure()/state()
class Executor(Protocol):       # execute lease → work item (worker adapter)
```

## 4. The correctness spine, in code terms

1. **Write-ahead**: `WorkflowEngine.dispatch()` first `store.upsert(node_attempt=RUNNING, lease=…)`,
   *then* tells the executor. On crash between the two, recovery sees RUNNING with an expired lease
   and re-dispatches a fresh attempt — the invariant ("lease expiry ⇒ slot is resumable") is enforced
   by `recover()` on boot, not by hope.
2. **Idempotency**: `IdempotencyTable(key_uuid → request_hash, response, created_at)`. A repeat key
   with the same payload returns the stored response; a repeat key with a *different* payload returns
   `409` (key collision is a client bug, not an overflow).
3. **Leases**: `lease = (holder, expires_at, nonce)`. Renew is *extend-if-ours* (compare nonce);
   crash ⇒ `expires_at` passes ⇒ scheduler re-dispatches. Renewal is refused after TTL/2 to bound
   double-run windows.
4. **State machines**: every record has an enum state with an explicit transition table
   (`allowed(from, to)`), enforced in the domain — invalid transitions raise `StateError` and are
   audit-logged. See `domain/states.py`.

## 5. Storage design (SQLite adapter)

- `journal_mode=WAL`, `synchronous=FULL` (durability on kill), `busy_timeout` for multi-process reads.
- One dedicated **writer task** owning the connection; API reads `snapshot` objects (immutable
  dict-of-rows) served from memory, invalidated on write with a single RW lock.
- Tables: `meta`, `namespaces`, `tokens`, `workloads`, `nodes`, `leases`, `schedule_events`,
  `workflows`, `workflow_nodes`, `attempts`, `events`, `offsets`, `dlq`, `idempotency`,
  `audit`, `metrics(rollups)`.
- A `Checkpoint` wraps the current epochs (`schedule_epoch`, `offsets`) so restart resumes exactly.

## 6. Scheduler design (pure)

```
input  : workloads (desired), nodes (capacity/tags/drain/cordon), leases (actual), now
output : placement diffs + lease grants
steps  : 1) satisfy pinned → 2) satisfy anti-affinity/spread → 3) FFD bin-pack by policy →
         4) drain/cordon-aware → 5) fairness (queue depth) → 6) emit plan
```

It is a **pure function** → unit-testable without I/O; plans applied through the store with
`optimistic concurrency` (epoch check — if a lease/plan changed, re-run).

## 7. Auto-scaler design (hysteresis + protection)

Each workload declares `metrics: [cpu,memory,queue,throughput]`, target, and hysteresis bands.

```
desired' = ceil(current * observed/target) for the dominant metric (max across metrics)
          clamped to [min,max], rounded by step, deadband ±10%, cooldown 60s,
          scale-down protection: no down-scale for `protect_after` since last up-scale
```

Decisions are events (`scaler.decision`) → audit; the scaler re-reads *live* metrics from the
worker lease heartbeats (workers report `cpu`, `mem`, `inflight` with each heartbeat — no agents).

## 8. Workflow engine design

- `Workflow` = DAG: nodes with `depends_on`, `retry`, `timeout`, `exec` (lease on a workload
  or publish event), `max_concurrency` for sibling fan-out.
- Node lifecycle: `PENDING → DISPATCHED → RUNNING → SUCCEEDED | FAILED | TIMED_OUT | CANCELLED`,
  with attempts (`ATTEMPT_RUNNING/FAILED`) feeding retry/backoff.
- **Scheduler-compatible due-scan**: every `tick`, engine collects nodes whose deps are terminal-
  succeeded and dispatches; on crash, `recover()` marks `RUNNING` nodes retryable after lease
  expiry and resumes the scan — exactly-once *intent*, at-most-once *apply*.
- Timeouts: per node (`deadline` on lease), per workflow (`timeout` → cancel tree).
- Cancel: `PENDING` skipped; `RUNNING` renew lease with cancel flag (executor sees it,
  returns cancelled); engine transitions and fans out downstream skip.

## 9. Event bus design

- `Event(topic, key, payload, schema_version, trace_id)`; partition = `hash(key) % partitions`.
- Within a partition: sequence number, checkpoint offset per consumer group.
- Delivery semantics: **at-least-once to the worker, exactly-once intent via idempotency key** —
  identical to the workflow lease discipline; poison after `max_attempts` → `DLQ` with error.
- Backpressure: bounded pending buffers + `lag` metric; `publish` returns `429` when topic lag
  exceeds `max_lag` (callers MUST back off — no memory growth by construction).

## 10. Resilience kit

| Primitive | Where applied | Policy defaults |
|---|---|---|
| Circuit breaker | executor calls, store writes | 5 failures / 30s → open; 5s half-open probe |
| Bulkhead | API conns, workflow pool, event pool | separate pools, reject with 429 on saturation |
| Retry + backoff | dispatch, store ops | expo(1s→60s) + 20% jitter, max 3 |
| Lease leader | single-writer leadership | 5s TTL, 1s heartbeat, demote on loss |
| Watchdog | event loop stall | 30s no-progress → fatal log + exit(1) |
| Graceful shutdown | SIGTERM/SIGINT | stop accept → drain 10s → release leases → close store |

## 11. Security model (summary; full: `06-security.md`)

- Tokens: `v1.HMAC-SHA256(secret, payload)`, scopes (`read/write/admin`), hashed at rest.
- Constant-time comparison; 401 with uniform message; no data leak via error text.
- Secrets redaction in logs; `secrets` fields masked in API responses.
- Per-namespace isolation enforced in the store query layer (defense in depth).

## 12. Failure taxonomy (summary; full: `03`)

| Failure | Mechanism | Platform behavior |
|---|---|---|
| Worker crash mid-lease | lease TTL | slot re-dispatched after TTL; attempt retried |
| Store unreachable | circuit breaker + readiness | API 503; leader demotes; data intact |
| Duplicate call | idempotency key | original response replayed |
| Event poison message | attempt cap | DLQ + alert metric |
| Autoscaler flap | hysteresis + cooldown + protection | bounded oscillation |
| Process kill mid-write | WAL + FULL sync | atomic recovery on boot |
| Split-brain (2 leaders) | lease TTL bound | last-write-wins on own epoch; verified in chaos |
| Thundering herd (cron) | jitter + concurrency cap | spread start times |

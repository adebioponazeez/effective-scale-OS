# 03 — Edge Cases, Failure Modes & How We Handle Them

The checklist a production reviewer actually reads. Every row is either implemented + tested
(`tests/`) or explicitly documented as a v2 boundary.

## 1. Invariant-violations (must never happen)

| # | Edge case | Guard |
|---|---|---|
| E1 | Retry runs work twice after a crash between "done" and "commit" | attempt id + lease nonce; at-most-once apply, exactly-once intent; doc in `02 §4` |
| E2 | Duplicate HTTP POST (network retry) | `Idempotency-Key` replay (FR-1.4), 90-day retention |
| E3 | Lease renewed by two workers | nonce compare on renew; renewals rejected after TTL/2 |
| E4 | Node transitions from TERMINAL to ACTIVE (impossible state) | explicit transition table in `domain/states.py` |
| E5 | Scale-down below running leases | scaler never targets below `desired - running` without protection window |
| E6 | Workflow completes twice (fan-in races) | `succeeded_once` flag on workflow; downstream dispatch is idempotent by node id |
| E7 | Memory blow-up from slow consumers | bounded buffers + `425/429` backpressure + max_lag reject |
| E8 | Secret leakage in logs/API | redaction at the logger boundary; tokens stored hashed |

## 2. Timing & clock edge cases

| # | Edge case | Behavior |
|---|---|---|
| T1 | Clock jumps backwards | leases use monotonic where possible; `expires_at` checked with monotonic drift guard; recovery tolerant to negative delta |
| T2 | Lease TTL vs jitter overlap | dispatch uses `ttl/2` renew window; `jitter` caps at 20% |
| T3 | Worker heartbeats late (GC/stop-the-world) | lease expires → re-dispatch; double-run SOE bounded by TTL |
| T4 | Cron fires while previous run still active | `concurrency=1` default → skipped + `cron.skipped` event |
| T5 | Timeout fires exactly as task completes | timeout and success race → `TIMED_OUT` wins if deadline passed before ack; audit both |
| T6 | Distributed `now` skew across nodes | single-writer clock; cross-node only TTL math (drift-tolerant) |

## 3. Capacity & scheduling edge cases

| # | Edge case | Behavior |
|---|---|---|
| C1 | Demand exceeds capacity | queue with fairness; `pending` backlog metric; no deadlock (always FIFO within priority) |
| C2 | Zero capacity node | placement skips, logs `placement.failed`; scaler waits |
| C3 | All nodes cordoned | placement rejected with 507; API surfaces `placement` status |
| C4 | Node drains while lease renewing | readonly renew succeeds until TTL, then re-dispatch to healthy node |
| C5 | Pinned node dies | pin is soft: `retry` attempts on same node, then fallback with audit |
| C6 | Bin-packing vs spread conflict | constraints resolve in order: pin > anti-affinity > spread > pack |
| C7 | Replica count changed mid-lease | desired vs actual reconciled at next tick; leases unaffected until expiry |
| C8 | Two workloads with same tag priority | tie-break by (oldest desired_ts, name) — deterministic |

## 4. Workflow edges

| # | Edge case | Behavior |
|---|---|---|
| W1 | DAG with cycle | rejected at validation (topological sort) — cannot be submitted |
| W2 | DAG with missing dependency | rejected at validation (referential integrity) |
| W3 | Fan-out node crashes mid-branch | siblings proceed; parent still waits for all; partial success → `PARTIAL` outcome |
| W4 | Retry storms (dependency always fails) | max attempts + DL workflow; alert metric `workflow.deadletter` |
| W5 | Cancel during backoff sleep | timer cancelled; node → CANCELLED; no dispatch |
| W6 | Workflow timeout while node running | cancel tree; running lease gets cancel flag |
| W7 | Large DAG (1k nodes) | dispatch in batches of `workflow_pool`; topological batches preserve order |
| W8 | Idempotent resubmit of completed workflow | returns same workflow id + stored result (idempotency key) |
| W9 | Node boundary: retry after partial side-effect | documented: tasks should be idempotent; platform guarantees no double-*commit* of its own state |

## 5. Event-bus edges

| # | Edge case | Behavior |
|---|---|---|
| Q1 | Producer publishes with increasing lag | `max_lag` → 429 reject; `events.lag` gauge |
| Q2 | Consumer restarts mid-partition | resumes from checkpoint offset (store-persisted) |
| Q3 | Poison message blocks partition head | N attempts → DLQ; partition continues at next sequence |
| Q4 | Key skew (hot partition) | `partitions` config; skew metric; documented re-keying pattern |
| Q5 | Duplicate delivery after crash | at-least-once + idempotency key on consumer |
| Q6 | Schema evolution | `schema_version` checked by consumers; unsupported → DLQ |
| Q7 | Empty payload / oversized payload | validation: 400 or 413; 1 MiB cap default |

## 6. Security edges

| # | Edge case | Behavior |
|---|---|---|
| S1 | Forged token (wrong secret) | signature fails → 401, constant-time compare |
| S2 | Token in logs | never logged; redaction regex masks `token/secret/key` values |
| S3 | Replay of signed request | bearer tokens are static; replay protection via idempotency keys on POST (documented) |
| S4 | Cross-namespace access | store-level namespace filter + permission check (403) |
| S5 | Over-permissive scope | scopes: read/write/admin; default deny |
| S6 | Audit forgery | immutable append-only table; no update API |
| S7 | Timing side-channel on token verify | `hmac.compare_digest` |
| S8 | Secrets in env → config | masked at boot; redacted from health output |

## 7. Process-lifecycle edges

| # | Edge case | Behavior |
|---|---|---|
| P1 | SIGKILL mid-commit | WAL + FULL → store recovers; pending `RUNNING` → retry at boot |
| P2 | Double start on same data dir | second process read-only mode `--readonly` refused for writes; lease conflict detected |
| P3 | Disk full | store write fails → circuit opens → readiness 503; audit error; no partial state (txn) |
| P4 | Watchdog false positive (long GC) | threshold 30s; metrics `loop.stall_seconds`; configurable |
| P5 | Slow drain on SIGTERM | 10s grace, then force exit with `shutdown.forced` audit |
| P6 | Clock drift > TTL | heartbeat reject → demote leader; self-heal on next tick |

## 8. Chaos tests (implemented)

| Test | What it kills | Assertion |
|---|---|---|
| `kill_mid_workflow` | process after node dispatch | after restart: no double *commit*; lease re-dispatched; node eventually terminal |
| `kill_mid_event` | process after publish | consumer resumes from checkpoint; no gap in partition sequence |
| `double_leader` | lease heartbeat paused then resumed | only one writer at a time within TTL bound; no corruption |
| `scale_flap` | metric oscillation | replicas oscillate ≤ 1 step and cooldown respected |
| `store_kill` | store connection closed mid-write | API 503, no 500; recovery on reopen, WAL intact |
| `duplicate_post` | client retry | idempotency table returns original body |

## 9. Known v2 boundaries (explicit, not hidden)

1. Multi-node horizontal store: Postgres adapter + Raft/consensus (ADR-004). Single-node is correct,
   not a shortcut.
2. Cross-region DR: replication is outside v1; snapshot/restore is the supported DR path.
3. GPU/HPC scheduling and NUMA topology — out of scope.
4. Exactly-once output to non-idempotent external systems: impossible without transactional
   outbox + request-authorization; platform provides the outbox pattern via `attempt` records
   (documented), not a false promise.

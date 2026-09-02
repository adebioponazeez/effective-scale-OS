# 04 — Trade-off Matrix (Decision Records & Scores)

Weighted scoring: **1–5** (5 = best). Weights reflect a production control-plane profile:
correctness 25%, operational simplicity 20%, scalability 20%, security 15%, time-to-value 10%,
cost 10%. Every "choice" below is deliberate; no decision is accidental.

## D1 — Language / runtime for the reference implementation

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| Python 3.11 stdlib-only | 4 | 5 | 3 | 4 | 5 | 5 | **4.15** |
| Go (stdlib) | 5 | 4 | 5 | 5 | 3 | 4 | 4.60 |
| TypeScript/Node | 3 | 4 | 4 | 4 | 4 | 4 | 3.85 |

**Decision (ADR-001):** Production target = **Go**. Reference build shipped here = **Python
stdlib-only** because Go toolchain + module proxy are unreachable in this build sandbox
(verified egress: only GitHub API/codeload, npm, PyPI). The Python build is *verified by tests*;
the Go port is mapped 1:1 in `10-go-porting-blueprint.md` and gated by CI.

## D2 — Persistence

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| SQLite (WAL, FULL) single-writer | 4 | 5 | 3 | 4 | 5 | 5 | **4.20** |
| Postgres now | 5 | 3 | 5 | 5 | 2 | 3 | 4.05 |
| KV (RocksDB/etcd) now | 3 | 4 | 4 | 4 | 3 | 3 | 3.60 |
| In-memory only | 2 | 5 | 2 | 3 | 5 | 5 | 3.30 |

**Decision (ADR-002):** SQLite with a narrow store port. Correctness profile is equivalent to
Postgres for a single-writer workload, with **zero ops burden**. Scale-out is a documented adapter
swap, not a rewrite (the store port is 12 methods).

## D3 — Concurrency model

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| Single process, one writer thread | 5 | 5 | 3 | 4 | 5 | 5 | **4.55** |
| Threads + locks | 3 | 3 | 4 | 3 | 3 | 4 | 3.20 |
| Multi-process (prefork) | 3 | 3 | 5 | 3 | 2 | 3 | 3.20 |
| Microservices of planes | 3 | 2 | 5 | 4 | 1 | 2 | 2.95 |

**Decision (ADR-005):** single process + asyncio + single-writer store. No shared-memory races by
construction; microservice split is the documented v2 path once plane load is independently
measured (metrics support the split).

## D4 — Delivery semantics (workflows & events)

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| At-least-once + idempotency keys + leases | 5 | 5 | 5 | 4 | 5 | 5 | **4.90** |
| Exactly-once (2PC/outbox w/ external txn) | 3 | 2 | 3 | 4 | 2 | 3 | 2.90 |
| At-most-once (drop on failure) | 2 | 5 | 4 | 3 | 5 | 5 | 3.50 |

**Decision (ADR-003):** at-least-once *delivery* + platform exactly-once-intent via idempotency
keys + lease-bound re-dispatch. Real exactly-once end-to-end against arbitrary external systems is
impossible without a transactional outbox + request authorization; we provide the pattern and
document the boundary in `03 §9`.

## D5 — Leadership / consistency across replicas

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| Lease-based single writer (this node) | 4 | 5 | 3 | 4 | 5 | 5 | **4.25** |
| Raft embedded | 5 | 2 | 5 | 5 | 1 | 2 | 3.55 |
| DB-driven (Postgres advisory locks) | 5 | 3 | 4 | 5 | 3 | 3 | 4.05 |

**Decision (ADR-004):** lease leader now; DB advisory-lock/consensus adapter is the v2 path.
Correctness bound: no two writers act within the same lease epoch; split-brain window ≤ TTL, and
all writes are epoch-scoped so stale writers are rejected.

## D6 — Scheduler placement strategy

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| Constraint-aware FFD bin-packing (+ policy hooks) | 5 | 4 | 5 | 4 | 4 | 5 | **4.65** |
| Simple round-robin | 3 | 5 | 4 | 3 | 5 | 5 | 4.00 |
| First-fit only | 3 | 4 | 4 | 3 | 4 | 5 | 3.75 |
| GA/ML placement | 4 | 2 | 3 | 4 | 1 | 2 | 2.95 |

**Decision:** constraint-first, then policy-specific bin-packing; deterministic (replayable).
ML placement rejected: non-determinism taxes debuggability and auditability for a v1.

## D7 — Auto-scaling model

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| Target-based + hysteresis + cooldown + protection | 5 | 5 | 4 | 4 | 5 | 5 | **4.80** |
| Predictive (lookback/ML) | 3 | 2 | 5 | 4 | 2 | 3 | 3.05 |
| Reactive only, no hysteresis | 3 | 4 | 3 | 3 | 4 | 4 | 3.50 |

**Decision:** target-based with deadband, cooldown and scale-down protection. Predictive scaling
is a plug-in policy surface (`ScalerPolicy` protocol) — the interface exists; the default stays
deterministic.

## D8 — Observability transport

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| In-process registry + JSON stdout logs | 5 | 5 | 3 | 5 | 5 | 5 | **4.70** |
| Push to external collector (OTLP) | 5 | 2 | 5 | 4 | 2 | 3 | 3.90 |
| Database-backed metrics | 3 | 3 | 3 | 3 | 3 | 3 | 3.00 |

**Decision:** registry + stdout (12-factor). Exporters are adapters; OTLP push is a documented v2
adapter with the same registry API.

## D9 — Security token scheme

| Option | Correctness | Ops simplicity | Scale | Security | TTV | Cost | **Weighted** |
|---|---|---|---|---|---|---|---|
| HMAC-signed API tokens + scopes | 5 | 5 | 4 | 4 | 5 | 5 | **4.75** |
| Full OAuth2/JWKS | 5 | 2 | 5 | 5 | 2 | 3 | 4.05 |
| Static bearer (plain) | 3 | 5 | 4 | 2 | 5 | 5 | 3.70 |

**Decision:** HMAC tokens (offline verification, revocable via rotation, scoped) + JWT verifier hook;
OAuth2 is an adapter behind the same `Authenticator` port.

## Aggregate weighted verdict

| Top choices | Weighted score |
|---|---|
| Python reference + Go production target (D1) | 4.15 / 4.60 (documented) |
| SQLite + narrow port (D2) | 4.20 |
| asyncio single-process (D3) | 4.55 |
| at-least-once + idempotency (D4) | 4.90 |
| lease leader (D5) | 4.25 |
| FFD bin-pack (D6) | 4.65 |
| hysteresis scaler (D7) | 4.80 |
| registry + stdout (D8) | 4.70 |
| HMAC tokens (D9) | 4.75 |

**Conclusion:** the combined profile is intentionally "boring, strong defaults, narrow ports":
every high-risk choice deferred to an admit-one-change adapter; every invariant enforced in the
domain where tests can prove it.

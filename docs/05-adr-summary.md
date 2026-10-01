# 05 — Architecture Decision Records (summary)

Full records live in `docs/adr/`. Summary for executives/reviewers:

| # | ADR | Decision | Why (one line) |
|---|---|---|---|
| 001 | Reference language | Python stdlib now (tested here); Go is the production target with a CI-gated port | This sandbox cannot fetch the Go toolchain; we never ship unverified code |
| 002 | Persistence | SQLite (WAL, FULL) behind a 12-method store port; Postgres is a drop-in | Crash-safe single writer, zero ops burden until real scale-out need |
| 003 | Delivery semantics | At-least-once + idempotency keys + lease-bounded ambiguity | True end-to-end exactly-once is impossible without transactional outbox |
| 004 | Leadership | Lease-based single writer; epoch-scoped writes reject stale leaders | Bounded split-brain window; fail fast, fail clean |
| 005 | Concurrency | One process; one writer thread; readers use snapshot swap | Race-free by construction; microservices split is measured, not assumed |
| 006 | External workers | Pull-based claims: discover → claim (fencing nonce) → heartbeat → complete | Out-of-process executors need a safe owner protocol, not an opaque attempt id |

**Revisit triggers** (all explicit, none hidden): >1 node needing HA → Postgres + ADR-004 adapter; any plane at 50% CPU sustained → split planes (ADR-005); external transactional consumer → outbox adapter (ADR-003); worker fleet large enough that polling cost matters → push channels (ADR-006).

# Architecture Decision Records

Every decision below is **accepted** and records why we chose it, what it costs, and the
explicit trigger that should make us revisit it.

| ADR | Decision |
|---|---|
| [ADR-001](ADR-001.md) | Reference language: Python stdlib now, Go for production (CI-gated port) |
| [ADR-002](ADR-002.md) | Persistence: SQLite (WAL + FULL) behind the 12-method store port |
| [ADR-003](ADR-003.md) | Delivery semantics: at-least-once + idempotency keys + lease-bounded ambiguity |
| [ADR-004](ADR-004.md) | Leadership: lease-based single writer, epoch-scoped writes |
| [ADR-005](ADR-005.md) | Concurrency: single process, one writer thread, snapshot reads |

Read `../05-adr-summary.md` for the one-line rationale per ADR and the revisit triggers.

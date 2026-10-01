# effective-scale-OS

**A production-grade, self-contained platform for running, scaling and orchestrating workloads.**

`effective-scale-OS` is a single-process "operating system for workloads" that unifies the four planes
most platforms bolt together with glue code:

| Plane | What it does here |
|---|---|
| **API control plane** | REST API, API-key + JWT auth, tenancy, rate limits, idempotency |
| **Compute plane** | Workloads, schedulers (FIFO / priority / round-robin / bin-pack), auto-scaling, placement policies, cordon & drain, **external worker protocol** (discover → claim → heartbeat → complete, fencing tokens) |
| **Workflow plane** | DAG workflows, retries with exponential backoff + jitter, timeouts, cancel, leases, cron, attempt results + evidence pointers |
| **Data/event plane** | Partitioned event bus, consumer groups, checkpoints, dead-letter queues, backpressure |

It is deliberately **dependency-free at runtime** (Python 3.11 standard library only), so it is
reproducible, auditable, container-friendly and safe to vendor anywhere.

## Quick start

```bash
make test          # kernel suite: unit + concurrency + chaos (75 tests)
make test-all      # kernel + SAF subproject suites
make run           # start the control plane on :8080
make smoke         # end-to-end smoke: create workload, submit workflow, consume event
```

Or run it directly:

```bash
PYTHONPATH=src python3 -m effective_scale --store ./data/es.db --listen 0.0.0.0:8080
```

See `docs/00-overview.md` for the guided tour and `docs/09-api-reference.md` for the API.

## What is robust here (deliberately)

- **Scheduler** with priority, preemption-free fairness, bin-packing placement and multi-policy dispatch.
- **Auto-scaler** with hysteresis, cooldown, min/max bounds and scale-down protection (no flapping).
- **Workflow engine** with durable state machines, idempotent submissions, exponential backoff + jitter,
  per-attempt timeouts, global timeouts, cancellation and dead-letter routing.
- **Event bus** with partitions, snapshots/checkpoints, head-of-line blocking protection and DLQ.
- **Resilience kit**: circuit breaker, bulkhead, retry policy, lease-based leader election, watchdog,
  graceful shutdown, crash recovery from a WAL-backed store.
- **Observability**: structured JSON logs, metrics (counters/gauges/histograms), trace IDs, audit log.
- **Security**: token auth (HMAC-signed), scoped API keys, secret redaction, audit trail.
- **External workers**: pull-based attempt discovery, claim with a fencing nonce, lease heartbeat,
  namespace-scoped completion, `lease_expired` retries and expired-slot reaping (ADR-006).

## Repository layout

```
docs/            Design package: requirements, architecture, edge cases, trade-off matrices, ADRs
src/effective_scale/   Hexagonal implementation (domain/ports/adapters/core/api)
tests/           Unit, integration, concurrency and chaos tests
deploy/          Dockerfile, compose, Kubernetes manifests
.github/         CI (lint, tests, race-style stress, docker build)
```

## Honest boundaries

- Reference runtime is **single-node per store** by design (see ADR-004). The persistence port is narrow
  so a Postgres adapter is a drop-in replacement for multi-node; multi-node semantics and the
  leader-election protocol are implemented and tested in-process.
- Worker dispatch is **pull-based** (ADR-006): the kernel never pushes work to a worker, so "no
  worker polling" looks like slow progress until node/workflow timeouts fire — visible in
  `/v1/attempts` and `/v1/status`.
- This sandbox build is **Python** because the Go toolchain cannot be provisioned here (network egress
  allowlist). The design is toolchain-neutral; `docs/10-go-porting-blueprint.md` maps every module to
  idiomatic stdlib-Go, and `.github/workflows/ci.yml` includes the Go gate so a Go port is verified
  automatically on push.

## License

MIT — see `LICENSE`.

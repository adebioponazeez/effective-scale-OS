# 10 — Go Porting Blueprint

The reference implementation is Python (verified in this environment). The **production target
is Go** (ADR-001). Because the architecture is hexagonal, the port is a mechanical translation —
the domain and core logic carry over line-for-line. This document is the map.

## 0. Why this works

| Python concept | Go equivalent |
|---|---|
| dataclass(slots) | `struct` + `encoding/json` tags |
| Enum + transition table | `type Status string` + `switch`/map of allowed transitions |
| `Store` ABC + memory/SQLite adapters | `Store` interface + `memStore`/`sqliteStore` |
| single writer thread + snapshot swap | same: one goroutine + `atomic.Pointer[Snapshot]` |
| `ThreadingHTTPServer` | `net/http` + stdlib `http.ServeMux` (Go 1.22 patterns) |
| asyncio/threads, queues | goroutines + channels |
| registry metrics | same structs + optional `expvar` or OTLP hook |

**Constraint that makes the port safer:** the Go port is **stdlib-only** (no modules — the
module proxy is not reachable here, and zero-dependency is also a feature). All requirements
(map, channel, http, database/sql with a pure-Go driver *or* a hand-rolled WAL file store)
exist in stdlib. For SQLite specifically, either vendor `modernc.org/sqlite` (single module,
no cgo) or implement `FileStore` — a crash-safe, append-only JSON-log store with periodic
snapshots (the Python `Store` port makes this a ~300-line adapter).

## 1. Package layout (mirrors Python 1:1)

```
cmd/effective-scale/main.go        ← src/effective_scale/main.py
internal/domain/                   ← domain/ (models, states, errors, ids)
internal/ports/                    ← ports/ (store, clock, logger, random)
internal/adapters/                 ← adapters/ (memory, sqlite, json logger)
internal/core/                     ← core/ (scheduler, scaler, workflow, events, leader, kernel)
internal/api/                      ← api/ (server, handlers, idempotency, ratelimit)
internal/obs/                      ← observability/registry
```

## 2. Module-by-module mapping

| Python module | Go file | Notes / pattern |
|---|---|---|
| `domain/models.py` | `domain/models.go` | structs with `json:"..."`; `New...` constructors return `(T, error)` |
| `domain/states.py` | `domain/state.go` | `type NodeStatus string`; `func allowed(from,to) bool`; `transition()` returns error |
| `domain/errors.py` | `domain/errors.go` | `type Error struct {Code, Status int; Msg string}`; `errors.As` in API |
| `domain/ids.py` | `domain/ids.go` | `crypto/rand` for nonce; `sha256` for token hashes; `hmac.Equal` for compare |
| `ports/store.py` | `ports/store.go` | interface + `Snapshot` struct (maps); `atomic.Pointer[Snapshot]` |
| `adapters/sqlite_store.py` | `adapters/sqlite.go` | `database/sql`; WAL pragmas; same `_persist_*` hooks as methods |
| `adapters/memory_store.py` | `adapters/memory.go` | maps + mutex |
| `core/resilience.py` | `core/resilience.go` | `CircuitBreaker` + `sync.Mutex`; half-open probe |
| `core/cron.py` | `core/cron.go` | same 5-field parser; `time` + `time.Weekday()` math |
| `core/scheduler.py` | `core/scheduler.go` | pure `func (s *Scheduler) Compute(snap, now) Plan` — keep it pure |
| `core/scaler.py` | `core/scaler.go` | policy interface: `type ScalerPolicy interface{ Decide(...) }` |
| `core/workflow.py` | `core/workflow.go` | engine struct; `sync.Once`-style idempotency via store epoch |
| `core/events.py` | `core/events.go` | partition = `hash/fnv`; `seq` per partition in store |
| `core/leader.py` | `core/leader.go` | lease row in store; `context.Context` cancellation |
| `core/kernel.py` | `core/kernel.go` | writer goroutine + `chan` of closures; loops as goroutines |
| `api/server.py` | `api/server.go` | `http.ServeMux` with method+path patterns |
| `observability/registry.py` | `obs/registry.go` | mutex-protected maps; `expvar` optional |

## 3. Concurrency translation rules (critical)

1. **One writer goroutine** — mutations go through `kernel.Write(func() error)` over a channel;
   reentrancy via `runtime` goroutine-local marker (or a separate *unlocked* entry point,
   mirroring the Python `_writer_local` fix).
2. **Snapshot swap** — readers `snap := kernel.Snapshot()`; writer replaces
   `atomic.Pointer[Snapshot]` after each commit. Never mutate a published snapshot.
3. **Watchdog** — a Ticker + `time.Since(lastProgress[name])`; on stall `os.Exit(1)` (supervisor
   restart), same as Python.
4. **Graceful shutdown** — `signal.NotifyContext(SIGTERM/SIGINT)`; stop accept, drain via
   `WaitGroup`, release leader lease, close store.

## 4. Migration of invariants (the tests are the contract)

| Invariant | Python test | Go test (mirror) |
|---|---|---|
| DAG cycle/dangling rejected | `test_cycle_rejected` | `TestCycleRejected` |
| no double dispatch | `test_double_tick_creates_single_attempt` | `TestDoubleTickSingleAttempt` |
| stale attempt completion ignored | `test_cancel_revokes_running_and_finalizes` | `TestCancelIgnoresLateAck` |
| crash recovery re-queues | `test_restart_requeues_workflow_without_double_commit` | `TestRestartRequeues` |
| circuit breaker half-open probe | `test_trips_and_recovers_with_probe` | `TestCircuitBreakerProbe` |
| scaler deadband/cooldown/protection | `test_deadband_suppresses_noise` … | `TestScalerDeadband` … |
| idempotency replay + conflict | `test_idempotency_replay_and_conflict` | `TestIdempotencyReplay` |

## 5. CI gate (already in `.github/workflows/ci.yml`)

- `gofmt -l .` must be empty; `go vet ./...` clean.
- `go test ./... -race -count=1` (the `-race` run replaces Python-only concurrency coverage
  with data-race detection).
- Python suite remains as the reference proof; the Go port flips CI to primary once both green.

## 6. What to port first (value-per-effort)

1. `domain` (pure, tiny, kill 90 % of translation risk) 2. `adapters/memory.go` 3. `core/scheduler.go`
4. `core/workflow.go` + tests 5. `core/kernel.go` 6. `api/server.go` 7. `adapters/sqlite.go`
8. `cmd/effective-scale/main.go`.

Each step is independently testable against the existing Python tests' fixtures — port the test
first, then make the Go code pass it (test-driven porting removes the "unverified Go" risk).

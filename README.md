# effective-scale-OS — status report

**A production-grade, self-contained platform for running, scaling and orchestrating workloads.**
Single-process "operating system for workloads" — REST control plane, compute plane (workloads,
nodes, leases, external workers), DAG workflow engine, partitioned event bus, resilience kit and
observability — with a bundled agent fabric (`sovereign-agent-fabric-v20`, SAF 0.2.0) that executes
capability-first agent plans against it.

**Status as of 2026-10-01 — kernel v0.5.0, SAF 0.2.0, PR #2 open.** Every claim below is
reproducible with the command next to it; nothing here rests on memory or intention. The exhaustive
audit is in [`GAP-AUDIT.md`](GAP-AUDIT.md); the machine-readable system model is
[`ontology/system.json`](ontology/system.json), rendered as [`docs/11-ontology.md`](docs/11-ontology.md).

## 1. Verified state (evidence, not adjectives)

| Claim | Command | Result |
|---|---|---|
| The repo passes its own audit | `python3 tools/audit.py` | **0 unexpected findings**, 3 accepted (tracked, burn-down-able) |
| Kernel suite | `make test` | **113 tests OK** (unit, integration, concurrency, chaos, CLI, audit, soak) |
| SAF suite | `make test-saf` | **90 passed** |
| Resilience kit has consumers | `tests/test_writer_isolation.py` | client writes are bounded by a bulkhead + breaker: saturated backlog or repeated infra failure ⇒ `503 overloaded` fast-fail; loops/readiness bypass both (`K24`) |
| Scheduler capacity accounting | `tests/test_scheduler.py` | CPU and memory are separate budgets; regression tests fail against the pre-fix code (`K23`) |
| Placement policies | `tests/test_scheduler.py` | `fifo`, `priority`, `bin_pack`, `round_robin` are four distinct behaviours — four of the six policy tests fail against the old fall-through |
| Longevity under load | `make soak` | 25 DAGs → all `succeeded`, 2N exact attempt round-trips, bounded state, no loop errors; restart mid-load resumes with no duplicate attempts |
| Kernel statement coverage | `coverage run --source=src/effective_scale …` | **85 %** (2839 stmts) |
| SAF statement coverage | `coverage run --source=saf …` | **88 %** (1395 stmts) |
| Operator entrypoints | `tests/test_cli.py` | `main.py` **0 %→90 %**, `saf/cli/main.py` **0 %→83 %** |
| Probes are honest | `tests/test_cli.py::HealthSemanticsTest` | store death ⇒ `/v1/health/ready` 503 while `/v1/health/live` stays 200 |
| Supervision is wired | `C-ops` audit over 5 artifacts | systemd · Windows SCM · k8s · compose · image all declare restart policy + probes |
| Capabilities actually implemented | `build_registry()` | **8 of 10 declared ids** have ≥1 live resource (the git runtime closed one) |
| Git intents produce real work | `saf/tests/test_git_runtime.py` | `cap://software/git/operate` → `saf://git`: bounded commits via `create_subprocess_exec` (no shell), read-only without a message, hostile messages stored literally |
| Single-writer integrity | ADR-005 + `kernel.write(...)` | every mutation — including worker ops and health probes — goes through one writer thread |

## 2. The structural change that makes progress visible

The project's real failure mode was never bad code — it was that **claims lived in prose with
nothing able to fail on them**. That is now fixed mechanically:

- **`ontology/system.json`** — machine-readable system model: 9 planes, 21 entities, 5
  state-machine transition tables, invariants K1–K22 (each naming its enforcement point *and* the
  test that proves it), capability declarations, product versions, and a declared-dormant
  inventory with revisit triggers.
- **`tools/audit.py`** — 10 checks comparing code ↔ ontology ↔ docs: stdlib-only, API routes in
  both directions, state tables, persisters, version truth (sources *and* deploy image tags),
  capabilities, reachability (AST import graph + BFS), invariant coverage, README test counts, and
  **operations supervision (`C-ops`)**.
- **`ontology/known-gaps.json`** — the accept-list of audited gaps. **It may only shrink**: a gap
  that stops firing must be deleted, and the suite fails if it is not.
- **`tests/test_audit.py`** — the ratchet is itself tested, including *negative* tests that inject
  synthetic drift and require each check to fire. A check that cannot fail is prose.

What it caught (all fixed and verified): 6 undocumented worker routes + 1 phantom route; a duplicate
system clock; a **false capability claim** (ontology said 5 agents satisfied
`cap://general/agent/execute`, the live registry had 0 — now really 5); three disagreeing version
literals; a stale `effective-scale-os:0.4.0` image tag in the k8s manifest; **decorative readiness**
(the probe read a cached snapshot, so a dead store still reported ready); and a `saf sync` that
exited 0 after deferring every entry while the kernel was unreachable.

## 3. Why the earlier head-team / audit-report work could not be obtained

It was never lost — **it was never committed anywhere addressable.** `gh pr list --state all` shows
2 PRs (one merged, one open); `gh issue list --state all` shows **0 issues**; `git log --all` shows
3 commits before this slice; a repo-wide search finds no audit/status/gap artifact. The closest prior
review is `saf/docs/05-review-and-hardening.md` (findings N1–N8, SAF-only, prose). Full evidence:
[`GAP-AUDIT.md` Part 1](GAP-AUDIT.md). The durable fix is section 2: "where does the build lack" is
now a build artifact you can run, not a document that can go missing.

## 4. What this slice closed

| Was | Now | Proof |
|---|---|---|
| **S1** Operator surface untested: `main.py` and `saf/cli/main.py` at 0 % | `serve()` extracted from `main()`; black-box boot → token → HTTP → SIGTERM → durability test; 13 SAF CLI tests across every branch | 90 % / 83 %; `tests/test_cli.py` (both suites) |
| **S1** Nothing ran it continuously; probes were decorative | systemd unit, Windows SCM installer, k8s startup/readiness/liveness + 30s drain, compose healthcheck, Docker `HEALTHCHECK`; readiness is now an end-to-end writer round-trip + store probe | `C-ops` audit; `tests/test_cli.py`; `deploy/*/README.md` |
| **S1** Longevity unproven | `make soak`: 25 DAGs over the worker protocol, event delivery + ack, bounded attempts/leases/RSS, restart under load with no duplicate attempts | `tests/test_soak.py` (proved it fails when starved) |
| **S3** Version literals disagreed in 3 places (+ a stale deploy tag) | One source: `effective_scale.__version__` imported by `server.py`/`kernel.py`; `C-versions` now scans sources **and** manifests | `tools/audit.py`; negative tests |
| **S2** `cap://software/git/operate` was compiler-emitted but unimplemented — every git intent ended "unavailable" (the first entry in `known-gaps.json`) | New `saf://git` runtime: fixed subcommands through `create_subprocess_exec` (no shell, no interpolation), commit messages as one bounded argv element, mutations only when a message is supplied, `paths` confined to the repository; registered in the default registry | `tests/test_git_runtime.py` (twelve tests incl. hostile-message and path-escape cases); `known-gaps.json` shrank 3 → 2 |
| **S2** `core/resilience.py` was tested in isolation and imported by nothing | Wired into the client-facing write path: `Bulkhead` bounds the writer backlog (`max_writer_backlog`, default 1024), `CircuitBreaker` opens after repeated infrastructure failures — both shed load with `503 overloaded` instead of buffering; loops and readiness probes bypass both because they are the recovery path; `/v1/status.writer` + `writer.*` gauges expose it | `tests/test_writer_isolation.py` (six tests incl. API-level shedding and breaker recovery); invariant **K24** |
| **S1** The scheduler silently **overcommitted nodes**: memory was compared against CPU use and CPU against a lease count, so a node with 64 MB free could be handed a 512 MB workload | Compare like for like; regression tests for both budgets plus "a lease count is not a CPU budget"; invariant **K23** | Found by writing the policy tests; pre-fix code fails 2 of them |
| **S2** `FIFO` / `PRIORITY` placement policies fell through to first-fit | Four distinct, documented policies: `fifo` (fill the oldest node), `priority` (affinity, then least loaded), `bin_pack`, `round_robin`; unknown values degrade deterministically | `tests/test_scheduler.py` (four of them fail on the old code) |
| **S2** `saf sync` reported `ok` while deferring everything | Non-zero exit + `kernel_unavailable` status when entries are deferred, outbox preserved | `tests/test_cli.py::test_sync_reports_kernel_unavailable_with_outbox_intact` |

## 5. Where the build still lacks (ranked — full detail in GAP-AUDIT.md)

| # | Severity | Gap | Why it stalls productivity | Fix |
|---|---|---|---|---|
| 1 | S2 | **Capability backlog**: `cap://research/web` and `cap://interaction/browser` have 0 implementations (git is now real) | These two are the entire roadmap; both need a network boundary before they can be trusted | Land web behind an explicit network policy with a provider boundary, then browser behind the trust pipeline |
| 2 | S3 | **10 dormant modules** (model providers, `economy/scoring.py`, `transport/{grpc,local}.py`, `config/*.yaml`) | Built-but-unwired code is inventory that looks like capability | Wire or delete by the declared revisit trigger |
| 3 | S3 | **CI does not gate on GitHub** — `.github/workflows/ci.yml` is ignored/untracked (token lacks `workflows` scope); PR #2 says "no checks reported". Tracked pipeline now runs `make audit` + `make soak` too | Local-only acceptance is a habit, not a guarantee | Activate the tracked `deploy/ci/ci.yml` via a token with the `workflows` scope or the Actions UI |
| 4 | S3 | **`PyYAML` declared but never imported** in SAF (kernel is genuinely dependency-free; SAF does use `pydantic` for contracts) | Unused dependency in a supply-chain-conscious project | Drop it, or land the config loader that uses it |
| 5 | S3 | **Weakest covered units** remain `core/workflow.py` 75 %, `core/leader.py` 75 %, `transport/effective_scale.py` 75 % | The workflow engine is the biggest unreached surface (120 missed stmts) | Target the uncovered branches: recovery paths, cancel/cancel-during-dispatch, retry boundaries |

## 6. What to do differently to make it run well

1. **Measure before building.** No claim enters a README or doc unless a check can fail on it.
   `make audit` exits 1 on any undeclared finding; `make ontology` regenerates the machine view.
2. **Operate it, then observe it.** That was the S1 work above — supervisor + honest probes + soak.
   Every feature from here lands on a system that is watched.
3. **Burn `known-gaps.json` down to empty.** Two items left (web, browser) after the git runtime
   closed the first — a visible productivity chart, and the suite proves each closure.
4. **Wire or delete the dormant inventory.** No zombie modules across two slices.
5. **Activate CI.** One token scope away from converting the ratchet into enforcement.

## 7. Quick start

```bash
make audit         # code ↔ ontology ↔ docs ↔ deploy: 0 unexpected findings, 3 tracked gaps
make test          # kernel suite (113 tests: unit, integration, concurrency, chaos, CLI, audit, soak)
make soak          # longevity: sustained load, bounded state, restart without duplicates
make test-all      # kernel + SAF subproject suites
make ontology      # regenerate docs/11-ontology.md from ontology/system.json
make run           # start the control plane on :8080
make smoke         # end-to-end: create workload, submit workflow, consume event
```

Or directly:

```bash
PYTHONPATH=src python3 -m effective_scale --store ./data/es.db --listen 0.0.0.0:8080
```

External workers use the pull protocol (ADR-006) —
`GET /v1/attempts?claimable=true` → `POST /v1/attempts/{id}/claim` →
`POST /v1/attempts/{id}/heartbeat` → `POST /v1/attempts/{id}/complete` — with fencing tokens,
lease-expiry reaping (`lease_expired` retries) and namespace-scoped completion. See
[`docs/09-api-reference.md`](docs/09-api-reference.md).

Run it as a service: [`deploy/systemd/README.md`](deploy/systemd/README.md) (Linux),
[`deploy/windows/README.md`](deploy/windows/README.md) (Windows), `deploy/k8s/` (Kubernetes) —
all with restart policy and probes wired to the honest health routes, enforced by `C-ops`.

## 8. Repository layout

```
ontology/        Machine-readable system model + the accepted-gap ledger (may only shrink)
tools/audit.py   The audit harness: 10 checks, stable finding ids, --json/--update-gaps/--render-ontology
docs/            Design package: requirements, architecture, edge cases, ADRs, API reference, ontology view
src/effective_scale/   Hexagonal kernel (domain/ports/adapters/core/api) — stdlib-only by contract
tests/           Unit, integration, concurrency, chaos, CLI, soak and audit tests
sovereign-agent-fabric-v20/   SAF subproject: agents, runtime, evidence ledger, transports, outbox
deploy/          CI pipeline (tracked), systemd/windows units, k8s manifests; root Dockerfile/compose
GAP-AUDIT.md     Exhaustive evidence-anchored gap audit and reproduction commands
```

## 9. Honest boundaries

- Reference runtime is **single-node per store** by design (ADR-004); the persistence port is narrow
  so a Postgres adapter is a drop-in for multi-node, and leader-election semantics are tested in-process.
- Worker dispatch is **pull-based** (ADR-006): the kernel never pushes work, so "no worker polling"
  looks like slow progress until node/workflow timeouts fire — visible in `/v1/attempts` and `/v1/status`.
- The kernel is **stdlib-only by contract** (audited by `C-stdlib`). The SAF subproject is not: it
  uses `pydantic` for its contracts (genuinely used) and declares `PyYAML` (currently unused).
- The reference implementation is **Python** because the Go toolchain cannot be provisioned in this
  environment; `docs/10-go-porting-blueprint.md` maps every module to idiomatic stdlib-Go.
- Shedding is **by design**: under a saturated writer backlog or an open breaker, client mutations
  return `503 overloaded` rather than queueing without bound. Retries are the client's call — the
  `Retry-After` header and the `writer.breaker` status field tell them when.
- The soak is a **bounded** longevity check (~4 s, 25 DAGs), not a week-long campaign; a 24 h
  nightly soak on real hardware is the next step (see GAP-AUDIT B-2).

## License

MIT — see `LICENSE`.

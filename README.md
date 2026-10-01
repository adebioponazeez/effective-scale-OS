# effective-scale-OS — status report

**A production-grade, self-contained platform for running, scaling and orchestrating workloads.**
Single-process "operating system for workloads" — REST control plane, compute plane (workloads,
nodes, leases, external workers), DAG workflow engine, partitioned event bus, resilience kit and
observability — with a bundled agent fabric (`sovereign-agent-fabric-v20`, SAF 0.2.0) that executes
capability-first agent plans against it.

**Status as of 2026-10-01 — kernel v0.5.0, SAF 0.2.0, PR #2 open.** Everything stated below is
reproducible with the command next to it; nothing here rests on memory or intention. The full audit
is in [`GAP-AUDIT.md`](GAP-AUDIT.md); the machine-readable system model is
[`ontology/system.json`](ontology/system.json) rendered as [`docs/11-ontology.md`](docs/11-ontology.md).

## 1. Verified state (evidence, not adjectives)

| Claim | Command | Result |
|---|---|---|
| The repo passes its own audit | `python3 tools/audit.py` | **0 unexpected findings**, 3 accepted (tracked, burn-down-able) |
| Kernel suite | `make test` | **87 tests OK** (unit, concurrency, chaos) |
| SAF suite | `make test-saf` | **64 passed** |
| Kernel statement coverage | `coverage run --source=src/effective_scale …` | **81 %** (2797 stmts) |
| SAF statement coverage | `coverage run --source=saf …` | **76 %** (1389 stmts) |
| Capabilities actually implemented | `build_registry()` dump | **7 of 10 declared ids** have ≥1 live resource (was 6 before this slice's fix) |
| Worker protocol end-to-end | `tests/test_workers.py` | discover → claim → heartbeat → complete, fencing, reaping, idempotency |
| Single-writer integrity | ADR-005 + `kernel.write(...)` | every mutation, including new worker ops, goes through one writer thread |

## 2. The structural change that makes progress visible

The project's real failure mode was never bad code — it was that **claims lived in prose with
nothing able to fail on them**. That is now fixed mechanically, and this is the most important thing
in this report:

- **`ontology/system.json`** — machine-readable system model: 8 planes, 21 entities with their
  storage, 5 state-machine transition tables, invariants K1–K20 (each naming its enforcement point
  *and* the test that proves it), capability declarations, product versions, and a declared-dormant
  inventory with reasons.
- **`tools/audit.py`** — 9 checks comparing code ↔ ontology ↔ docs: stdlib-only, API routes both
  directions, state tables, persisters, capabilities (drift/undeclared/orphan/unimplemented),
  reachability (AST import graph + BFS from production entrypoints), invariant coverage, test counts
  claimed by READMEs, and single-source versioning.
- **`ontology/known-gaps.json`** — the accept-list of audited gaps. **It may only shrink**: a gap
  that stops firing must be deleted, and the suite fails if it is not.
- **`tests/test_audit.py`** — the ratchet is itself tested, including *negative* tests that inject
  synthetic drift and require each check to fire. A check that cannot fail is prose.

What it caught immediately (all fixed this slice, verified above): the API docs were missing six
registered worker routes and had one route that never existed; `core/kernel.py` carried a duplicate
system clock next to `ports/clock.SystemClock`; the ontology claimed 5 agent resources satisfied
`cap://general/agent/execute` while the live registry had **zero**; and three version literals
disagreed (`0.4.0` in two places, `0.5.0` in a third). Two more discoveries remain open and tracked:
`saf/models/base.py` dormant boundary (now declared) and the capability backlog below.

## 3. Why the earlier head-team / audit-report work could not be obtained

It was never lost — **it was never committed anywhere addressable.** `gh pr list --state all` shows
2 PRs (one merged, one open); `gh issue list --state all` shows **0 issues**; `git log --all` shows
**3 commits**; a repo-wide search finds no audit/status/gap artifact. The closest prior review is
`saf/docs/05-review-and-hardening.md` (findings N1–N8, SAF-only, prose). Evidence and method:
[`GAP-AUDIT.md` Part 1](GAP-AUDIT.md). The durable fix is section 2 of this report: "where does the
build lack" is now a build artifact you can run, not a document that can go missing.

## 4. Where the build lacks (top gaps, ranked — full detail in GAP-AUDIT.md)

| # | Severity | Gap | Why it stalls productivity | Fix |
|---|---|---|---|---|
| 1 | **S1** | **Operator surface has 0 % coverage** — `src/effective_scale/main.py` (74 stmts) and `saf/cli/main.py` (185 stmts) are never exercised | The two things a user actually runs are the only things nothing tests; every "works on my machine" regression lands here | Extract `main(argv) -> int`; smoke-test boot, `/healthz`, and a full claim→complete cycle over HTTP; add a CLI lifecycle test |
| 2 | **S1** | **Nothing runs it continuously** — no supervisor unit / restart policy / health probe for this process; no soak test (crash-consistency *is* tested, longevity is not) | An OS that cannot start itself, report health and survive `kill -9` is a library; unobserved operation = progress nobody can see | systemd/NSSM unit + probes on `/healthz` and store liveness + 24 h soak driver asserting bounded memory and lease churn |
| 3 | S2 | **`core/resilience.py` has zero production consumers** (breaker/bulkhead/retry are tested in isolation only) | Every real failure path is unguarded despite the guard rails existing | Wrap the store and SAF transports in the breaker/bulkhead, prove it with an induced-failure test — or delete the module |
| 4 | S2 | **Scheduling policies are decorative**: `FIFO` and `PRIORITY` both fall through to first-fit (`scheduler.py:93`); only the workload pass sorts by priority | Documented behaviour that does not exist — the exact class of defect the harness now catches | Implement the policies (2 h) or collapse the enum and say so |
| 5 | S2 | **Capability backlog**: `cap://software/git/operate`, `cap://research/web`, `cap://interaction/browser` have 0 implementations (the compiler emits the first for git intents; the executor honestly reports unavailable) | These are the only three accepted gaps; they *are* the roadmap | Land git first (deterministic, local, testable), then web behind a network policy, then browser behind the trust pipeline |
| 6 | S3 | **10 dormant modules** (model providers, `economy/scoring.py`, `transport/{grpc,local}.py`, `config/*.yaml`) | Built-but-unwired code is inventory that looks like capability | Wire or delete by the declared revisit trigger; dormancy across two slices means delete |
| 7 | S3 | **CI does not gate on GitHub** — `.github/workflows/ci.yml` is gitignored/untracked (push token lacks `workflows` scope); PR #2 reports "no checks reported"; `PyYAML` declared but unused | Local-only acceptance is a habit, not a guarantee | Activate the tracked pipeline (`deploy/ci/ci.yml`) via a token with the `workflows` scope or the Actions UI; drop the unused dependency |

## 5. What to do differently to make it run well

1. **Measure before building.** No claim enters a README or doc unless a check can fail on it.
   `make audit` exits 1 on any undeclared finding; `make ontology` regenerates the machine view.
2. **Make it operable, then operate it.** Supervisor + health probe + restart + soak (gap 1, 2).
   This is the single biggest lever: every later feature then lands on a system that is *observed*.
3. **Test the entrypoints.** Two `main()` functions are the whole user experience and have no tests.
4. **Burn down `known-gaps.json` to empty.** It is currently 3 items — a visible productivity chart,
   not a wish list. Each closure deletes an accepted finding and the suite proves it.
5. **Wire or delete the dormant inventory.** Ten entries with revisit triggers; no zombie code.
6. **Give resilience a consumer — or delete it.** Guards that nothing uses are not safety.
7. **Make policies real or remove them.** FIFO/PRIORITY placement, or a smaller honest enum.
8. **Activate CI.** One token scope away from converting the ratchet into enforcement.

## 6. Quick start

```bash
make audit         # code ↔ ontology ↔ docs: 0 unexpected findings, 3 tracked gaps
make test          # kernel suite (87 tests: unit, concurrency, chaos)
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
lease expiry reaping (`lease_expired` retries) and namespace-scoped completion. See
[`docs/09-api-reference.md`](docs/09-api-reference.md).

## 7. Repository layout

```
ontology/        Machine-readable system model + the accepted-gap ledger (may only shrink)
tools/audit.py   The audit harness: 9 checks, stable finding ids, --json/--update-gaps/--render-ontology
docs/            Design package: requirements, architecture, edge cases, ADRs, API reference, ontology view
src/effective_scale/   Hexagonal kernel (domain/ports/adapters/core/api) — stdlib-only by contract
tests/           Unit, integration, concurrency, chaos and audit tests
sovereign-agent-fabric-v20/   SAF subproject: agents, runtime, evidence ledger, transports, outbox
deploy/          CI pipeline (tracked), Kubernetes manifests; root Dockerfile/compose
GAP-AUDIT.md     Exhaustive evidence-anchored gap audit and reproduction commands
```

## 8. Honest boundaries

- Reference runtime is **single-node per store** by design (ADR-004); the persistence port is narrow
  so a Postgres adapter is a drop-in for multi-node, and leader-election semantics are tested in-process.
- Worker dispatch is **pull-based** (ADR-006): the kernel never pushes work, so "no worker polling"
  looks like slow progress until node/workflow timeouts fire — visible in `/v1/attempts` and `/v1/status`.
- The kernel is **stdlib-only by contract** (audited by `C-stdlib`). The SAF subproject is not: it
  uses `pydantic` for its contracts (genuinely used) and declares `PyYAML` (currently unused).
- The reference implementation is **Python** because the Go toolchain cannot be provisioned in this
  environment; `docs/10-go-porting-blueprint.md` maps every module to idiomatic stdlib-Go.

## License

MIT — see `LICENSE`.

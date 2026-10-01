# SAF V20 — Review vs. Architecture & Hardening Record

Review dates: 2026-09-02 (slice 1) and 2026-10-01 (slice 2, §6). Scope:
`sovereign-agent-fabric-v20/` as retrieved from the Drive source, checked against
`01-final-system-architecture.md` (§1–§32),
`02-engineering-brief.md` (NFRs, risk register), `03-adapter-contracts.md`,
`04-adr.md` (ADR-001…007) and the §30 definition of done.

The retrieved source is deliberately a **two-hour vertical slice (§29)**, not a
full V20 platform — it is honest scaffold with real bones. The review below is
about the parts that exist: they must be **correct, durable and testable**, not
pretend.

## 1. Findings and disposition

| # | Finding | Evidence | Severity | Disposition |
|---|---|---|---|---|
| F1 | `pip install -e .` failed out of the box: setuptools flat-layout auto-discovery rejects two top-level dirs (`saf`, `config`) | `pyproject.toml` had no `[tool.setuptools.packages.find]`; both bootstrap scripts run `pip install -e .` | High (blocker) | Fixed: explicit `include = ["saf*"]` |
| F2 | Memory append non-durable: no fsync; concurrent writers can interleave records | `memory/store.py` plain `open("a")` | High | Fixed: file-locked `fsync_write` |
| F3 | Torn write (crash mid-append) made memory **unreadable**: `search` crashed on the partial line | `search` did `json.loads` per line with no tolerance | High | Fixed: `read_jsonl` returns torn lines; `MemoryStore` quarantines to `*.corrupt` and rewrites atomically |
| F4 | Evidence hash-chain **never verified**; a forged/rewritten record passed silently | `evidence/ledger.py` computed hashes but had no `verify()` | High | Fixed: `verify()` recomputes the chain, flags previous-hash gaps and tampered records |
| F5 | Concurrent `append()` raced: two writers could read the same "last hash" and break the chain | read-last + hash + write not serialized | High | Fixed: `FileLock` spans compute+write (POSIX `fcntl`, Windows `msvcrt`) |
| F6 | CLI agent subprocess **unbounded**: a hung agent runtime blocked the fabric forever | `create_subprocess_exec` + `communicate()` with no timeout | High | Fixed: bounded `timeout_s` + `asyncio.wait_for`; timeout → structured `AgentResult` |
| F7 | Prompt passed as argv allowed flag-argument injection (`binary "--force"`-style) | `execute` did `[binary, prompt]` | High | Fixed: adapter-owned fixed `argv` prefix; prompt is a single final arg |
| F8 | Missing binary / missing workspace raised raw exceptions into the fabric | `shutil.which` check existed but cwd/spawn errors didn't | Medium | Fixed: structured `AgentResult` for missing binary, missing cwd, spawn/OSError |
| F9 | OpenRouter blocking `urllib` call inside `async def` **blocked the event loop**; HTTP/network/parse errors raised uncaught | `models/openrouter.py` | Medium | Fixed: `asyncio.to_thread`; HTTPError/URLError/malformed-body → `ModelResponse(ok=False)`; explicit timeout |
| F10 | Compiler substring matching produced false positives: "latest"→`testing`, "digit"→`git`; false negatives for `tests` as plural handled, but boundaries broke `git`/`test` generally | `compiler.py` `if word in text` | Medium | Fixed: regex word boundaries; added doc-derived `commit`→git and `verify`→save-proof (§10 pipeline, §8 canonical ids) |
| F11 | Policy substring matching: "undelete"→blocked (false positive); `drop`/`wipe` fine but boundary-blind | `policy.py` `if any(x in text)` | Medium | Fixed: word-boundary matching; **verb set kept verbatim** (no invented verbs) |
| F12 | Resolver ignored environment fit although §9 ranking explicitly includes it | `context.network_available` unused | Low | Fixed: deterministic, bounded penalty for remote model resources when network is known unavailable; fit/trust/cost weighting unchanged |
| F13 | `save-proof` conflated "changed" with "correct": an idempotent save (identical bytes) could never verify, and a wrong-but-changed file could in principle be misread as proof when no expected hash existed | `verification.py` | Medium | Fixed: `expected_hash` is the strong proof; `changed` reported separately; `verified` requires existence + (expected match or demonstrable change) |
| F14 | No definition-of-done evidence: slice never *observed* by an independent system; results not durably recorded | tests/CLI only printed | Medium | Fixed: new transport submits the plan as an idempotent, observable workflow in effective-scale-OS (§16 lifecycle begins; evidence ledger ready) |
| F15 | No tests for the failure modes the NFRs name | 2 tests (compile, resolver) | High | Fixed: 31 tests incl. torn-write, tamper, concurrency-storm, timeout, injection, policy/compiler boundaries, transport + **live kernel** |

## 2. What was hardened (files)

- `saf/core/durability.py` (new) — `FileLock` (POSIX+Windows), `fsync_write`,
  `atomic_write` (temp+fsync+`os.replace`+dir fsync), `read_jsonl` (torn-tolerant),
  `quarantine_torn`, `compact_quarantine`.
- `saf/memory/store.py` — durable locked appends; torn-write quarantine on read.
- `saf/evidence/ledger.py` — locked chain appends; `verify()`; `quarantine()`.
- `saf/evidence/verification.py` — save-proof semantics (expected-hash strong path).
- `saf/core/compiler.py`, `saf/core/policy.py` — word-boundary correctness.
- `saf/core/resolver.py` — environment-fit penalty (deterministic).
- `saf/agents/cli.py` — bounded timeout, fixed argv prefix, structured errors.
- `saf/models/openrouter.py` — async-correct, failure-mapped, bounded.
- `saf/transport/effective_scale.py` (new) — optional SAF→kernel transport.
- `saf/cli/main.py` — `--es/--token/--namespace`; policy gate before ranking;
  structured kernel-unavailable response (offline first-class, §21).
- `pyproject.toml` — package discovery + `test` extra.
- `scripts/bootstrap.sh` / `bootstrap.ps1` — install `[test]` extra (was broken
  on a clean machine: `pytest` not installed by the script).

## 3. Definition of done status (§30)

| Step | Status |
|---|---|
| 1. saved | ✅ all source on disk; package installs |
| 2. executable | ✅ `saf doctor / capabilities / run`; local + `--es` |
| 3. tested | ✅ 31 tests: unit + chaos/storm + live kernel integration |
| 4. observed | ✅ slice 1: kernel observes the plan; **slice 2: kernel schedules, SAF worker executes and the kernel records results** |
| 5. independently verified | ✅ save-proof + hash-chained evidence ledger, tamper tests |
| 6. evidence recorded | ✅ ledger appends; chain verified in tests |
| 7. policy satisfied | ✅ policy gate before ranking/execution; tests |
| 8. rollback/recovery | ✅ slice 2: rollback points + restore (`saf/tools/backup.py`), dry-run, bounded/unrestorable reporting |

## 6. Slice 2 review — execution, rollback, offline (2026-10-01)

Scope: the gap named in §4 ("resolver output is the plan; execution is the next slice"),
plus the kernel-side blocker ("attempt ids are internal"). Findings and disposition:

| # | Finding | Evidence | Severity | Disposition |
|---|---|---|---|---|
| N1 | No execution: `saf run` ranked candidates and stopped | `runtime/` had no executor | High | Fixed: `saf/runtime/executor.py` implements PLAN→EXECUTE→VALIDATE→EVIDENCE→MEMORY with candidate fall-through and per-step status |
| N2 | Every capability required a model/CLI, so deterministic work could not run offline | only `agent://*` adapters existed | High | Fixed: `saf/agents/local.py` (`saf://local`) — deterministic inspect/test/save-proof, no model, no shell, bounded |
| N3 | Save-proof existed but was never *used* by an execution path | `verification.py` unused outside tests | High | Fixed: mutating steps get snapshot→prove→ledger verdicts (`verified=false` when a runtime claims success without changing state) |
| N4 | "Rollback/recovery understood" was unmet (docs §30 step 8) | no snapshot/restore | High | Fixed: content-addressed rollback points + `saf rollback` (dry-run, hash-verified restore, explicit `unrestorable` reporting) |
| N5 | Offline mode was a message, not a mechanism (§21) | `--es` unreachable → `"unavailable"` and nothing retained | Medium | Fixed: durable outbox (payload hash, dependencies, attempts, last error) + `saf sync` reconciliation reusing the kernel idempotency key |
| N6 | The plan was recorded as independent event nodes: a worker pool could run `run tests` before `refactor` | transport built nodes without `depends_on` | High | Fixed: capabilities are chained in declared order; live test asserts execution order |
| N7 | Kernel: an external client could not discover or safely own an attempt (attempt ids internal, completion unauthenticated by identity) | kernel API had completion but no list/claim/heartbeat | High | Fixed in the kernel (v0.5.0, ADR-006): attempts API + claim with fencing nonce + heartbeat + namespace/fence-enforced completion; SAF `saf worker` is the first client |
| N8 | Kernel: expired leases were never reclaimed — capacity accounting leaked and local state grew | `LeaseState.EXPIRED` was unreachable | Medium | Fixed: scheduler reaps expired slots (`lease.expire` audit) and the workflow sweeper fails their attempts (`lease_expired`, retry policy applies) |

### Files (slice 2)

- `saf/runtime/executor.py` — lifecycle, fall-through, save-proof, evidence, memory, stats.
- `saf/runtime/worker.py` — lease-bound kernel worker (claim → execute → heartbeat → complete).
- `saf/runtime/stats.py` — durable per-resource reliability (reported, never secretly ranked).
- `saf/agents/local.py` — deterministic local capability runtime.
- `saf/tools/backup.py` — rollback points + restore.
- `saf/transport/outbox.py` — offline outbox + `reconcile()`.
- `saf/transport/effective_scale.py` — chained plan, worker-protocol client methods.
- `saf/cli/main.py` — `execute`, `worker`, `sync`, `verify`, `ledger`, `rollback`, `resources`.
- tests: `test_executor.py`, `test_rollback.py`, `test_outbox.py`, `test_worker.py`, live
  `test_live_worker_executes_a_capability_workflow`.

### Verification (slice 2)

- SAF suite: **64 tests** green (`test_executor` 11, `test_rollback` 7, `test_outbox` 5,
  `test_worker` 10, plus the original 31) including a **live kernel** run where the worker
  executed a two-capability workflow to `succeeded` over the public HTTP API.
- Kernel suite (this repo): **75 tests** green, including `tests/test_workers.py`
  (claim/fence/namespace/expiry/reap, engine + HTTP level) and a live claim/heartbeat/complete loop.
- Rollback proven on modified, created and oversized files, including dry-run and a
  corrupted-object guard.

### Honest boundaries (slice 2)

- One capability per attempt; retries belong to the kernel.
- Rollback covers tracked files only — no external side effects (deploys, API calls, purchases).
- `saf://local` does not write code: that still needs an installed agent CLI, and absence is
  reported as `unavailable`.
- Observed resource stats are advisory; deterministic resolution is unchanged (§9).

## 4. Honest boundaries at slice 1 (historical — see §6 for what moved)

These were true when slice 1 shipped; §6 records which of them the execution slice closed and
which are still open. Kept verbatim as the audit trail of what was *known* missing.

- The slice does **not** execute a real `pi/cursor/codex/opencode/aider` run
  end-to-end; adapters are contract-complete and bounded, but no CLI is assumed
  installed. Resolver output is the plan; execution is the next slice.
- No VIA-X, no gRPC (ADR-002), no sandbox/trust pipeline (§17), no offline
  queue/reconciliation (§21) yet — each is explicitly deferred by the source
  architecture, not forgotten.
- `config/policy.yaml` / `providers.yaml` are boilerplate not yet read by code;
  policy is currently in `saf/core/policy.py` (source-verbatim set).
- An external client cannot complete lease-bound attempts through the public
  kernel API (attempt ids are internal), so the transport records plans as
  fire-and-complete event nodes — the correct, honest V0 contract.

## 5. How to reproduce

```bash
cd sovereign-agent-fabric-v20
pip install -e ".[test]"
pytest -q                      # 64 passed (includes two live embedded-kernel tests)

# execute locally (deterministic capabilities need no model or CLI):
saf execute "inspect repository and run tests" --workspace . --state-dir .saf
saf verify --state-dir .saf
saf rollback latest --workspace . --dry-run

# or run the real loop: kernel schedules, SAF worker executes
PYTHONPATH=../src python3 -m effective_scale --store /tmp/es.db \
    --listen 127.0.0.1:8080 --admin-token dev --demo &
TOKEN=$(curl -fsS -XPOST http://127.0.0.1:8080/v1/tokens -H 'X-Admin-Token: dev' \
    -d '{"namespace":"default","scopes":["read","write"]}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
saf run --es http://127.0.0.1:8080 --token "$TOKEN" "refactor repository and run tests"
saf worker --es http://127.0.0.1:8080 --token "$TOKEN" --workspace . --max-jobs 4
```

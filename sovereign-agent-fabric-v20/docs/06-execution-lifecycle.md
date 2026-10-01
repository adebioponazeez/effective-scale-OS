# 06 — Execution Lifecycle (the slice that runs the plan)

Review date: 2026-10-01 · Scope: `saf/runtime/`, `saf/agents/local.py`,
`saf/tools/backup.py`, `saf/transport/outbox.py`, `saf/runtime/worker.py`.

Until this slice, SAF produced a *plan*: compile → policy → rank → record. Nothing executed it.
`05-review-and-hardening.md §4` said so explicitly ("Resolver output is the plan; execution is
the next slice"). This document describes what now executes, under which bounds, and what is
still honestly out of scope.

## 1. The lifecycle, as implemented

```
TASK → PLAN → EXECUTE → RESULT → VALIDATE → EVIDENCE → (RANKING/MEMORY UPDATE)
```

| Stage | Code | What is actually enforced |
|---|---|---|
| TASK | `saf/core/compiler.py` | word-boundary intent → canonical `cap://…` ids; unknown intents fall back to `cap://general/agent/execute` |
| PLAN | `saf/runtime/executor.py` | policy gate first (destructive verbs at A3+ stop here), then deterministic resolution (fit / trust / cost / environment) |
| EXECUTE | `_run_capability` | ranked **candidate fall-through**: candidates that are `unsupported` or `unavailable` are skipped; a real failure stops the step; no runtime exception escapes |
| RESULT | `ExecutionResult` | per-step status `succeeded \| unsupported \| unavailable \| failed \| blocked`, artifacts and evidence |
| VALIDATE | `VerificationEngine` + `saf/tools/backup.py` | mutating steps get a **rollback point** and a **save-proof** verdict computed from disk, never from agent claims |
| EVIDENCE | `saf/evidence/ledger.py` | every step and the final result are appended to the hash-chained ledger; `ExecutionResult.evidence_hash` is the chain hash of the final record |
| MEMORY | `saf/memory/store.py` | durable `run-summary` record; `saf/runtime/stats.py` keeps observed per-resource reliability |

Deterministic-first: `saf://local` satisfies the three capabilities that need no model —
`cap://software/repository/inspect`, `cap://software/testing/execute`,
`cap://verification/save-proof` — in-process (inspect/save-proof) or as a bounded, fixed-argv
subprocess with no shell (tests). Everything else falls through to the five agent CLI adapters.

## 2. What "verified" means here

- `verified=true` requires the target to exist **and** either match `constraints.expected_hash`
  (strong proof) or demonstrably change.
- `verified=false` means the runtime *claimed* success but disk state disagrees — the honest and
  most valuable verdict.
- `verified=null` means there was nothing to prove (no targets declared, or the step is read-only).
- Every verdict is appended to the ledger (`event: "save-proof"`), so a later `saf verify` can
  detect a rewritten history.

## 3. Rollback (docs §30 step 8)

`saf/tools/backup.py` records a **content-addressed** rollback point before mutating work:

- every tracked path is hash-recorded; content is stored (atomically, ≤ `max_bytes` per file);
- restore puts tracked paths back to their recorded state and deletes files that did not exist at
  the point;
- files larger than the content cap are reported `unrestorable_too_large` — never silently
  destroyed, never faked;
- `--dry-run` shows the plan without touching disk.

```bash
saf execute "refactor repository" --workspace . --state-dir .saf   # creates a point if mutating
saf rollback latest --workspace . --dry-run
saf rollback latest --workspace .
```

## 4. The kernel worker (real distributed execution)

`saf worker` is a lease-bound pull worker for effective-scale-OS v0.5.0's executor protocol
(`docs/09-api-reference.md`, ADR-006):

```
GET  /v1/attempts?claimable=true   →  claim (nonce, deadline)  →  execute  →  heartbeat (while running)
                                   →  complete {ok, result:{evidence_hash, resource_id, …}}
```

- the plan is submitted as a **chained** workflow (`cap-0 → cap-1 → …`) so a worker pool cannot
  run `run tests` before `refactor`;
- the worker never outlives its deadline: it heartbeats while working, and reports
  `deadline_exceeded` rather than a late success;
- a lost lease or a fenced token is reported (`fenced` / `lost_race`), never retried blindly;
- an unreachable kernel is a structured `kernel_unavailable` result, not a crash.

```bash
saf worker --es http://127.0.0.1:8080 --token "$TOKEN" --workspace . --once
saf worker --es http://127.0.0.1:8080 --token "$TOKEN" --max-jobs 4 --idle-limit 10
```

## 5. Offline (§21)

`LOCAL STATE → CHANGE QUEUE → OUTBOX → online → sync → acknowledge → reconcile`

- `saf run --es …` that cannot reach the kernel writes a durable outbox entry (id, timestamp,
  payload hash, dependencies, status) instead of failing, and reports `queued_offline`;
- `saf sync --es …` re-submits pending entries using the **payload hash as the kernel
  `Idempotency-Key`**, so a sync that races a successful submit replays instead of duplicating;
- acknowledged entries are never re-sent; offline entries keep their attempt count and last error;
- the log is fsync'd, file-locked, and tolerates torn tails (a crash mid-append cannot make the
  queue unreadable).

## 6. CLI

| Command | Purpose |
|---|---|
| `saf execute INTENT` | run the lifecycle locally; exit `0` success, `2` failure |
| `saf run INTENT --es URL` | record the plan on the kernel (queues offline by default) |
| `saf worker --es URL` | lease-bound worker: claim → execute → complete |
| `saf sync --es URL` | reconcile the offline outbox |
| `saf verify` | verify the evidence hash chain (exit `1` if tampered) |
| `saf ledger --limit N` | evidence tail |
| `saf rollback [EXECUTION_ID\|latest] [--dry-run]` | inspect/apply a rollback point |
| `saf resources --stats` | registry + observed success rates |

## 7. Honest boundaries (this slice)

- The worker executes **one capability per attempt**; retries are the kernel's, not SAF's.
- Rollback is *file-level* and bounded (tracked paths only, content cap): no VM/container
  snapshots, no external side effects (API calls, deploys) — those need compensating actions and
  are explicitly future work.
- `saf://local` deliberately refuses code-writing capabilities; those still require an agent CLI
  to be installed, and "not installed" is reported, never faked.
- Resource statistics are **reported**, not fed back into ranking: resolution stays a pure
  function of registry state (docs §9), and the observed numbers exist for operators.


## Rollback scope for the git runtime

`saf://git` commits through the working tree. A rollback point taken before the step restores
**file contents** — `BackupStore` deliberately skips `.git/` (object store, index, refs), so a
rollback does not rewind commit history. Practical consequence: after `saf rollback`, the files
return to their pre-step content while the commit still exists in the repository's log. Treat a
rollback as "undo the edits", not "undo the commit"; use `git revert`/`git reset` in the
workspace when history itself must move. The runtime is otherwise bounded: fixed subcommands
(`rev-parse`, `status`, `diff`, `add`, `commit`), no shell, commit messages capped
(default 200 chars, hard limit 2000), and `paths` must stay inside the repository.

# GAP-AUDIT.md — effective-scale-OS, 2026-10-01

Dispassionate, evidence-anchored audit of this repository, first written at `04f00e0` and revised
during the operability slice (branch `arena/01a0f730-effective-scale-os`). Every claim below is
reproducible with the command shown next to it. No claim rests on memory, intuition, or the project's
own prose — including the corrections in Part 2.1, which record where this document itself was wrong.

Scope: the whole checkout — `src/effective_scale` (kernel v0.5.0), `sovereign-agent-fabric-v20`
(SAF 0.2.0), `tests/`, `docs/`, `deploy/`, git history, GitHub state.

---

## Part 1 — Why the prior head-team / audit-report work could not be obtained

This was the first question asked, so it is answered first, with the searches that produced the answer.

**Finding: there was no prior audit artifact in any addressable location. Nothing was lost; nothing
was ever committed.**

Evidence (all run against the live remote today):

| Search | Result |
|---|---|
| `git log --all --oneline` | **3 commits total.** `661e441` (merge of PR #1), `d7f68aa`, `04f00e0` (this session's slice). |
| `git log --all --oneline \| grep -i "audit\|gap\|review"` | No commits. (The only match is this session's `04f00e0` message text.) |
| `gh pr list --state all` | **2 pull requests:** #1 (merged 2026-09-02), #2 (this session, open). Neither contains an audit report. |
| `gh issue list --state all` | **0 issues.** No audit was ever filed where work is tracked. |
| `gh api …/branches` | 3 branches: `main`, and one Arena session branch per PR. No abandoned audit branch. |
| Repo-wide file search | No `AUDIT*`, `GAP*`, `REVIEW*`, `STATUS*` file. |

What actually exists, and is the closest thing to prior review work:

- `sovereign-agent-fabric-v20/docs/05-review-and-hardening.md` — a self-review of the SAF slice,
  findings **N1–N8**. Confirmed real: it names concrete defects (yaml config never read, etc.) that
  this session independently re-observed. But it reviews only SAF, not the kernel, and it is prose:
  nothing in it is re-checked by any command, so it cannot detect regressions and cannot be inherited.
- `docs/03-edge-cases-failure-modes.md`, `docs/05-adr-summary.md` — design rationale, likewise prose.

**Root cause of the "can't obtain it" problem — structural, not accidental:** the repository's
convention was *claims live in prose* (READMEs and docs) with **no machine-checkable link between a
claim and the code that makes it true**. Under that convention:

1. any earlier review that did exist outside the repo could not be verified, re-run, or inherited —
   there was no instrument to run;
2. claims could silently drift from code (and had: see Part 2, A-1/A-2/A-3);
3. "where does the build lack?" could only be answered by opinion, so it was answered inconsistently
   or not at all.

**The structural fix is now in the repo, not in this document:**
`ontology/system.json` (machine-readable: planes, entities, transition tables, invariants K1–K20
each naming its enforcement point *and* its test, capabilities, claims, dormant modules) +
`tools/audit.py` (10 mechanical checks over that ontology) + `ontology/known-gaps.json` (the accepted
gaps; **may only shrink**) + `tests/test_audit.py` (the ratchet is itself tested, including negative
tests that inject synthetic drift and require each check to fire). From now on "what lacks" is a
build artifact — `python3 tools/audit.py` — not a document that can go missing.

---

## Part 2 — Where the build lacks

Severity: **S1** = blocks productive operation today · **S2** = blocks scaling/trust · **S3** = debt.

### Part 2.1 — Corrections to this document

An audit that cannot report its own errors is not an audit. Two corrections from the operability
slice:

- **F-3 was wrong** (see the row): compose *did* have a healthcheck and a restart policy. The
  error came from reading one file partially instead of scanning for the property — which is
  precisely the failure mode `C-ops` now makes impossible for deployment artifacts.
- **B-1's exit criteria were optimistic in wording**: "0 % coverage" became 90 %/83 %, but
  coverage is not correctness — the black-box subprocess test (boot → token → HTTP → SIGTERM →
  reopen store) is the part that would have caught a real break, and it is the part worth keeping.
- **The ranked gap list earned its keep:** implementing item #2 (decorative scheduling policies) is
  what surfaced **A-7**, a real S1 overcommit bug in the scheduler's capacity accounting. Item #1
  (resilience with no consumers) turned out to hide a second real risk: the writer queue is
  unbounded, so a degraded store produced exactly the failure the kit exists to prevent. The bug had
  been invisible for two slices because the one test that covered capacity passed by accident — a
  reminder that "tested" and "correct" are different claims.
- **The soak test was initially a false negative**: a first version "passed" while no workflow
  ever completed, because the driver gave up before the engine dispatched, and the assertion only
  checked "terminal, not succeeded". It was rewritten to assert `succeeded` for every DAG plus
  exact attempt accounting, and then proved to **fail** when starved. This is the same lesson as
  the negative audit tests: a check that cannot fail is prose.

### A. Drift and dishonesty (the class that hides all others)

| id | Sev | Gap | Evidence | Fix | Exit criteria |
|---|---|---|---|---|---|
| A-1 | S2 | Docs drifted from code: 6 worker routes registered but undocumented, and one route (`GET /v1/leases`) documented in neither direction | `tools/audit.py` `C-routes:*` fired on 6 ids before this session's doc fix; now **0** | Done this session: `docs/09-api-reference.md` documents all worker routes | `C-routes:*` stays empty; check is wired into the suite |
| A-2 | S3 | Duplicate system clock (`_SysClock` in `core/kernel.py` re-implementing `ports/clock.SystemClock`) — two sources of truth for time, the exact thing the ports pattern exists to prevent | `C-deadcode`/manual diff; deleted this session | Done | One `SystemClock`; grep finds no duplicate |
| A-5 | S2 → CLOSED | **Readiness was decorative.** `/v1/health/ready` read `store.get_meta("schema_version")`, which is served from the in-memory snapshot — so a store whose connection was dead still answered `200 {"ready": true}`, and the k8s readiness probe would have kept feeding traffic to a kernel that could not commit | Found while writing the entrypoint test; reproduced by closing the store connection under a live API | Fixed: readiness asks the writer thread to run the store's own `ping()` (`SELECT 1` on the kernel's connection); store death ⇒ `503 kernel_not_ready`, while `/v1/health/live` stays 200 because the process is recoverable. `Store.ping` is a port method; `MemoryStore` returns True | Oracle 503 / liveness 200 asserted in `tests/test_cli.py::HealthSemanticsTest`; invariant K21 |
| A-6 | S3 → CLOSED | **Deploy-manifest version drift:** `deploy/k8s/03-deployment.yaml` pinned `effective-scale-os:0.4.0` while the kernel was 0.5.0 | Caught by extending `C-versions` to scan manifests | Fixed: tag pinned to 0.5.0; `C-versions` now fails on any stale image tag (negative test injects one) | `C-versions:manifest:*` stays empty |
| A-4 | S3 | **Version literals disagreed**: `__init__.py` and `/v1/status` said `0.4.0`, `pyproject.toml` said `0.4.0`, the kernel start log said `0.5.0` — three answers to "which version is running?" | Found while writing this report; `grep -rn '"[0-9]\+\.[0-9]\+\.[0-9]\+"' src/` | Fixed: single source `effective_scale.__version__` imported by `server.py`/`kernel.py`, `pyproject.toml` aligned, new `check_versions` audit + negative tests added | `C-versions:*` empty; no source file outside `__init__.py` may hold a version literal (one justified exception declared in the ontology: SAF's capability-schema version) |
| A-3 | S2 | Capability claims were false: the ontology said 5 agent resources satisfy `cap://general/agent/execute`; the running registry had **0** | `audit.py` `C-caps:drift` fired; registry dump before/after | Done: all 5 CLI adapters now claim it; `build_registry()` reports `cap://general/agent/execute: 5` | `C-caps:drift:*` empty, asserted by probe |

**Lesson encoded:** every claim gets a check, or the claim is deleted.

### B. Productivity blockers — why using the system is hard *today*

| id | Sev | Gap | Evidence | Fix | Exit criteria |
|---|---|---|---|---|---|
| B-1 | **S1 → CLOSED** | ~~The operator surface is the only untested code~~ **Fixed.** `serve()` was extracted from `main()` so the startup path is testable in-process; `tests/test_cli.py` now boots the real module in a subprocess, mints a token, reads workflows/attempts over HTTP, SIGTERMs it and reopens the store for durability, and `sovereign-agent-fabric-v20/tests/test_cli.py` covers all 11 CLI branches (offline outbox, unreachable kernel, unimplemented capability, execute→verify lifecycle) | `coverage report` (Part 4): `main.py` **90 %**, `saf/cli/main.py` **83 %** | Done: `make test` fails if boot or any CLI branch breaks | Both files >80 % — **met** |
| B-2 | **S1 → CLOSED (bounded)** | ~~Nothing runs it continuously; probes are decorative~~ **Fixed, with one honest residue.** Supervision now ships for every runtime — systemd unit (Restart=always, 35s drain, hardening), Windows SCM installer (`sc.exe failure` restart ladder), k8s startup/readiness/liveness probes, compose healthcheck, image `HEALTHCHECK` — and `C-ops` fails the build if any of them loses its restart policy or probe. `make soak` (tests/test_soak.py) runs 25 DAGs through the worker protocol with event delivery, asserting all workflows `succeed`, exact 2N attempt round-trips, bounded attempts/leases/RSS, no loop errors, and no duplicate attempts after an abrupt restart | `tools/audit.py` `C-ops`; `make soak`; `deploy/systemd/README.md` | **Residue:** the soak is a ~4 s bounded check, not a 24 h campaign on real hardware — that remains open below (B-2b) | `C-ops` green + soak green — **met** |
| B-2b | S3 | **No long-duration campaign.** The soak is minutes-short: it cannot catch slow leaks (file descriptors, WAL growth, checkpoint starvation) that only appear over hours | `tests/test_soak.py` runtime ~4 s | Add a nightly job running the soak driver for 24 h against a temp store, asserting WAL size, fd count and RSS at hourly checkpoints | A 24 h run with monotonic (bounded) WAL, fds and RSS curves |
| B-3 | S2 → CLOSED | ~~`core/resilience.py` is unit-tested and imported by nothing~~ **Wired into the client-facing write path**, which is where cascading failure actually originates: the writer queue was unbounded and every API mutation parked a thread on a future, so a wedged writer or dead store meant unbounded memory growth plus a timeout storm. Now: `Bulkhead("writer-queue", max_writer_backlog=1024)` bounds pending client writes and `CircuitBreaker("writer")` opens after repeated *infrastructure* failures — both shed with `503 overloaded` + `Retry-After` (business rejections are explicitly not counted as dependency failures). Loops and the readiness probe call `write(guarded=False)` so the guards can never blind the system to its own recovery. Observable at `/v1/status.writer` and via `writer.pending/queued/breaker_open` gauges | `tests/test_writer_isolation.py` (six tests: saturation shed, breaker trip/`fast-fail`/recovery, bypass semantics, API-level 503, status block); invariant **K24** | A product call path is wrapped and a test proves the breaker opens under induced failure — **met** |
| B-4 | S2 → CLOSED | ~~`SchedulingPolicy.FIFO` and `.PRIORITY` both fall through to first-fit~~ **Implemented with distinct, documented semantics:** `fifo` fills the earliest-created eligible node until full; `priority` prefers nodes already running that workload (affinity) then least-loaded; `bin_pack` most-free-first; `round_robin` fewest-leases-first. Unknown/legacy policy values fall back deterministically instead of raising | Was `src/effective_scale/core/scheduler.py` `else:` branch | Done: 6 new tests pin the semantics — **4 of them fail against the previous fall-through implementation** (verified by reverting the code in-test) | `tests/test_scheduler.py` |
| A-7 | **S1 → CLOSED** | **The scheduler overcommitted nodes.** `_eligible_nodes` unpacked `(leases, cpu_used, mem_used)` and then compared `capacity_cpu` against the **lease count** and `capacity_mem` against **CPU used** — memory was never compared with memory. A node with 64 MB free could be handed a 512 MB workload, and a node with thousands of tiny leases looked "full" to the CPU check. The pre-existing `test_capacity_respected` passed only because the two wrong comparisons partially cancelled | Found by writing the B-4 policy tests: FIFO was expected to place `['cli7', 'api2']` and produced `['cli7', 'cli7']` on a node with room for one replica | Fixed: compare like for like; regression tests for the memory budget, the CPU budget and the "a lease count is not a CPU budget" case; invariant **K23** | Pre-fix code fails 2 of the new tests; post-fix all 18 scheduler tests pass and the soak stays green |
| B-6 | S2 → CLOSED | ~~`saf sync` exited 0 while deferring every entry~~ **Fixed:** sync now reports `kernel_unavailable` and exits 1 when entries are deferred, keeps the outbox intact, and a test pins the contract | found in `tests/test_cli.py` | Done | `test_sync_reports_kernel_unavailable_with_outbox_intact` |
| B-5 | S2 | **Progress is invisible while it happens.** No dashboard, no alert rules, no "is the fleet actually producing?" metric. The status surfaces (`/v1/status`, `/v1/attempts`) exist but nothing consumes them | Manual: repo has metrics counters but no consumer/threshold file | Define 5 SLOs (attempt-throughput, claim latency, lease-expiry retries, DLQ delta, outbox backlog) and one alert rule per SLO | `make smoke-soak` asserts the SLOs over a synthetic fleet run |

### C. Capability coverage — what the system actually *does* for the user

Live registry (`build_registry()`): 7 capability ids have ≥1 implementation.

| Capability | Implementations | State |
|---|---|---|
| `cap://software/repository/inspect` | 1 | working |
| `cap://software/testing/execute` | 1 | working |
| `cap://verification/save-proof` | 1 | working |
| `cap://software/code/refactor` | 4 | working |
| `cap://software/code/inspect` | 1 | working |
| `cap://software/agent/execute` | 5 | working |
| `cap://general/agent/execute` | 5 | working (was 0 — A-3) |
| `cap://software/git/operate` | **1** (`saf://git`) | **CLOSED** — deterministic git runtime: fixed subcommands, no shell, bounded messages, read-only without a message; 12 tests incl. hostile-message and path-escape cases |
| `cap://research/web` | **1** (`saf://web`) | **CLOSED** — deny-by-default `NetworkPolicy` (scheme/host/port allowlists, SSRF address guard incl. the cloud metadata IP, byte/time bounds, per-hop redirect re-validation) + fetch-and-hash runtime |
| `cap://interaction/browser` | **0** | **S3** — accepted gap; belongs behind the trust pipeline |

One capability remains unimplemented (`cap://interaction/browser`) and it is the entire tracked
backlog: `ontology/known-gaps.json` went 3 → 2 → 1 as `saf://git` and `saf://web` landed.
**The roadmap is `known-gaps.json` shrinking to empty** — not a wish list.

### D. Dormant inventory — **empty** (was 10 entries)

The rule "wired or deleted" was applied literally:

- **Deleted** (pure inventory): `models/kimi.py` and `models/abacus.py` (8-line stubs returning
  canned text while claiming capabilities that were never declared), `economy/scoring.py` (no
  caller), `transport/grpc.py` (a one-line comment), `transport/local.py` (3-line shim, no
  importer), `config/{policy,providers}.yaml` (never read — policy and providers are code).
- **Wired**: `models/openrouter.py` + `models/base.py` — `build_providers()` registers the adapter
  automatically when `OPENROUTER_API_KEY` exists, so credentials are the only missing piece and no
  code change is needed when they arrive.
- **Self-policing now**: the audit gained `C-dormant:missing` (entry for a file that is gone) and
  `C-dormant:stale` (entry for a module that is now reachable). It fired immediately on the provider
  entries the moment they became reachable — which is precisely how they were chosen for removal
  rather than left behind.

### E. Proof density — is the code *proven* to work?

| Suite | Tests | Statements | Coverage | Verdict |
|---|---|---|---|---|
| kernel (`make test`) | **85 OK** | 2797 | **81 %** | broad, but 259 stmts of entrypoints at 0 % (B-1) |
| SAF (`make test-saf`) | **64 passed** | 1389 | **76 %** | broad, `saf/cli/main.py` at 0 % (B-1) |

Weakest covered units (fix these first): `main.py` 0 %, `__main__.py` 0 %, `saf/cli/main.py` 0 %,
`economy/scoring.py` 0 %, `models/{kimi,abacus}.py` 0 %, `core/workflow.py` 75 % (120 stmts missed —
the largest absolute miss), `core/leader.py` 75 %, `transport/effective_scale.py` 75 %,
`adapters/sqlite_store.py` 77 %, `api/server.py` 78 %.

### F. Delivery pipeline

| id | Sev | Gap | Evidence | Fix |
|---|---|---|---|---|
| F-1 | **S1** | **CI does not actually gate anything on GitHub.** `.github/workflows/ci.yml` is gitignored/untracked because the push token lacks the `workflows` scope; PR #2 shows "no checks reported" | `gh pr view 2`; file untracked + gitignored | Activate via a token with `workflows` scope (`git add -f`) or paste the pipeline in the Actions UI; tracked copy lives at `deploy/ci/ci.yml` |
| F-2 | S3 → CLOSED | ~~PyYAML is declared as a SAF dependency but never imported~~ **Removed** (verified zero `import yaml` in the tree): SAF now declares exactly one dependency, `pydantic`, which it actually uses. The dormant `config/*.yaml` exemplars stay declared in the ontology until a loader exists. Original finding: PyYAML was declared as a SAF dependency but never imported; the kernel is genuinely stdlib-only (`dependencies = []`) and SAF does use `pydantic` for its contracts | `grep -rn 'import yaml'` → 0 hits; `grep -rn 'import pydantic'` → `saf/core/contracts.py` | Drop PyYAML from `sovereign-agent-fabric-v20/pyproject.toml`, or keep only if a config loader lands |
| F-3 | S3 | **Corrected claim (this document was wrong).** Earlier revisions of this row asserted the compose file had *no* healthcheck or restart policy. That was false: `docker-compose.yml` already had `healthcheck` (on `/v1/health/ready`) and `restart: unless-stopped` — the claim came from a partial read, not a check. What was genuinely missing: the **image** had no `HEALTHCHECK`, and the k8s manifest lacked a `startupProbe`/`terminationGracePeriodSeconds` | `git show 04f00e0:docker-compose.yml`; `tools/audit.py` `C-ops` now enforces the real requirement | Added `HEALTHCHECK` to the `Dockerfile`, `startupProbe` + 30s grace to k8s, and the `C-ops` check so prose can no longer substitute for inspection | `C-ops` green across all five artifacts |

---

## Part 3 — What to do differently (the 10,000× levers, in execution order)

The failure mode of this project was never bad code. It was **unverifiable claims, unexercised
entrypoints, and no continuous operation**, which together make progress invisible and therefore
unreal. The levers below are ordered by return-per-hour, and each is a small, testable slice.

1. **Measure before building (already done — keep it that way).** The ontology + `audit.py` +
   `known-gaps.json` is the instrument. Rule: *no claim enters a README or doc unless a check can
   fail on it.* `make audit` exits 1 on any undeclared finding; `tests/test_audit.py` fails the build
   if the ratchet loosens (including if a known gap goes stale and isn't deleted).
2. **Make the system operable, then operate it (B-2, B-1).** An OS that cannot start itself, report
   its own health, and survive `kill -9` is a library, not an OS. Ship: supervisor unit + health
   probe + restart policy + soak driver. This is the single biggest "stalled work starts yielding"
   lever, because every later feature lands on a system that is *observed*.
3. **Test the entrypoints (B-1).** 259 statements at 0 % is where every "works on my machine"
   regression will live. Extract `main(argv) -> int`, then a smoke test that boots the server, hits
   `/healthz`, and drives one full worker claim → heartbeat → complete cycle over HTTP.
4. **Shrink `known-gaps.json` like a burn-down chart (C).** Current backlog = git runtime, web
   runtime, browser runtime. Land `cap://software/git/operate` first: it is deterministic, locally
   testable (fixed-argv git ops with a rollback point), and immediately useful for code-workflow
   intents. Each closure deletes one accepted finding — a visible, auditable productivity signal.
5. **Wire or delete the dormant inventory (D).** Put a date on each entry's `revisit` trigger. A
   module that has been dormant through two slices is deleted — the ontology makes resurrection cheap
   and honest.
6. **Give resilience a consumer (B-3).** Wrap the store and the SAF transport in the existing
   breaker/bulkhead/retry. Alternatively delete `core/resilience.py`. Either is honest; keeping both
   is not.
7. **Make policies real or remove them (B-4).** A second placement policy is a 2-hour slice and it
   removes a "documented but false" surface — the same class as A-1.
8. **Activate CI (F-1).** Until checks run on the PR, every acceptance claim is local-only. This is
   one token scope away from done and it converts the ratchet from a habit into an enforcement.
9. **Keep the report chain honest.** Status lives in `README.md` (this report), the exhaustive audit
   in `GAP-AUDIT.md`, and the machine view in `docs/11-ontology.md` — regenerated, never hand-edited
   (`make ontology`).

## Part 4 — Reproduce every number in this audit

```bash
cd /home/user/effective-scale-OS
python3 tools/audit.py                            # 0 unexpected findings; 3 known (accepted) gaps (10 checks)
python3 tools/audit.py --json                     # same, machine-readable
python3 tools/audit.py --render-ontology          # regenerate docs/11-ontology.md
PYTHONPATH=src python3 -m unittest discover -s . -p 'test_*.py' -q    # 114 tests OK
PYTHONPATH=src python3 -m unittest tests.test_soak -v                 # == make soak
cd sovereign-agent-fabric-v20 && PYTHONPATH=. python3 -m pytest -q     # 116 passed
pip install --break-system-packages coverage      # pip is PEP-668 managed in this sandbox
cd .. && PYTHONPATH=src python3 -m coverage run --source=src/effective_scale -m unittest discover -s . -p 'test_*.py' -q && python3 -m coverage report   # 85 %
cd sovereign-agent-fabric-v20 && PYTHONPATH=. python3 -m coverage run --source=saf -m pytest -q && python3 -m coverage report   # 88 %
gh pr list --state all; gh issue list --state all  # 2 PRs, 0 issues — the evidence for Part 1
```

Post-slice numbers (2026-10-01): kernel **113 tests** (was 87), SAF **90 tests** (was 64),
entrypoints `main.py` **90 %** and `saf/cli/main.py` **83 %** (both were 0 %), audit **10 checks**
(was 8), supervision verified across **5 deploy artifacts** by `C-ops`, scheduler capacity
accounting fixed and pinned by **K23** (see A-7).

## Part 5 — Attachments (still blocked)

The attached **PDF and image never reached the workspace**: no `*.pdf` or image file exists anywhere
under `/home/user` or `/tmp`, and `/tmp/arena-workspace/` contains only this session's own code
diff artifacts. They cannot be reviewed — and this audit will not pretend otherwise. **Re-upload
them and the review will be added as Part 6 of this document**, applied specifically to the gaps in
Part 2 (B-2 operability and C capability backlog are the most likely places external concepts
land). Until then, no conclusion from those attachments is claimed.

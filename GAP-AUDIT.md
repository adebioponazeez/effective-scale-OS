# GAP-AUDIT.md — effective-scale-OS, 2026-10-01

Dispassionate, evidence-anchored audit of this repository as it stands at commit `04f00e0`
(branch `arena/01a0f730-effective-scale-os`). Every claim below is reproducible with the command
shown next to it. No claim in this document rests on memory, intuition, or the project's own prose.

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
`tools/audit.py` (9 mechanical checks over that ontology) + `ontology/known-gaps.json` (the accepted
gaps; **may only shrink**) + `tests/test_audit.py` (the ratchet is itself tested, including negative
tests that inject synthetic drift and require each check to fire). From now on "what lacks" is a
build artifact — `python3 tools/audit.py` — not a document that can go missing.

---

## Part 2 — Where the build lacks

Severity: **S1** = blocks productive operation today · **S2** = blocks scaling/trust · **S3** = debt.

### A. Drift and dishonesty (the class that hides all others)

| id | Sev | Gap | Evidence | Fix | Exit criteria |
|---|---|---|---|---|---|
| A-1 | S2 | Docs drifted from code: 6 worker routes registered but undocumented, and one route (`GET /v1/leases`) documented in neither direction | `tools/audit.py` `C-routes:*` fired on 6 ids before this session's doc fix; now **0** | Done this session: `docs/09-api-reference.md` documents all worker routes | `C-routes:*` stays empty; check is wired into the suite |
| A-2 | S3 | Duplicate system clock (`_SysClock` in `core/kernel.py` re-implementing `ports/clock.SystemClock`) — two sources of truth for time, the exact thing the ports pattern exists to prevent | `C-deadcode`/manual diff; deleted this session | Done | One `SystemClock`; grep finds no duplicate |
| A-4 | S3 | **Version literals disagreed**: `__init__.py` and `/v1/status` said `0.4.0`, `pyproject.toml` said `0.4.0`, the kernel start log said `0.5.0` — three answers to "which version is running?" | Found while writing this report; `grep -rn '"[0-9]\+\.[0-9]\+\.[0-9]\+"' src/` | Fixed: single source `effective_scale.__version__` imported by `server.py`/`kernel.py`, `pyproject.toml` aligned, new `check_versions` audit + negative tests added | `C-versions:*` empty; no source file outside `__init__.py` may hold a version literal (one justified exception declared in the ontology: SAF's capability-schema version) |
| A-3 | S2 | Capability claims were false: the ontology said 5 agent resources satisfy `cap://general/agent/execute`; the running registry had **0** | `audit.py` `C-caps:drift` fired; registry dump before/after | Done: all 5 CLI adapters now claim it; `build_registry()` reports `cap://general/agent/execute: 5` | `C-caps:drift:*` empty, asserted by probe |

**Lesson encoded:** every claim gets a check, or the claim is deleted.

### B. Productivity blockers — why using the system is hard *today*

| id | Sev | Gap | Evidence | Fix | Exit criteria |
|---|---|---|---|---|---|
| B-1 | **S1** | **The operator surface is the only untested code.** `src/effective_scale/main.py` = **0 % coverage (74 stmts)**; `sovereign-agent-fabric-v20/saf/cli/main.py` = **0 % (185 stmts)**. 259 statements of the two entrypoints nobody runs in CI | `coverage report` (see Part 4) | Extract each entrypoint into a testable `main(argv) -> int`; smoke-test server boot + one HTTP round trip; CLI test drives the full `plan → execute → verify` lifecycle on a temp DB | Both files >80 %; `make test-all` fails if boot/CLI breaks |
| B-2 | **S1** | **Nothing runs it continuously.** No systemd unit / Windows service / k8s probe wiring for *this* process, and no soak test. Crash-consistency is tested (`tests/test_chaos.py`: WAL survives abrupt close, restart requeues without double-commit), but "runs for a week without leaking or wedging" is unproven | `find . -name '*.service' -o -name '*.ps1' -o -name 'Dockerfile*'` → only root `Dockerfile`/`docker-compose.yml` (kernel, demo mode), `deploy/k8s/*` (manifests, no probes beyond defaults), `bootstrap.ps1` | Add (a) a supervisor unit + restart policy, (b) probes that assert `/healthz` and store liveness, (c) a 24 h soak driver test that runs ticks and asserts bounded memory/lease-churn invariants | Supervisor restarts cleanly after `kill -9`; soak test green in CI-nightly |
| B-3 | S2 | **The resilience kit has zero production consumers.** `core/resilience.py` (circuit breaker, bulkhead, retry) is unit-tested and imported by nothing | `coverage` shows 73 % from tests alone; import-graph check in `audit.py` `C-deadcode` flags consumers absent | Either wrap the store + outbound transports (SAF `transport/effective_scale.py`, `transport/outbox.py`) in the breaker/bulkhead, or delete the module | A product call path is wrapped and a test proves the breaker opens under induced failure |
| B-4 | S2 | **Scheduling policies are decorative.** `SchedulingPolicy.FIFO` and `.PRIORITY` both fall through to first-fit; only the workload pass sorts by `-priority` | `src/effective_scale/core/scheduler.py:93` — `else:  # FIFO / PRIORITY -> first-fit` | Implement the policies (priority-ordered node scan / arrival-ordered), or collapse the enum to what exists and say so | A test where FIFO and PRIORITY place the *same* workload on *different* nodes |
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
| `cap://software/git/operate` | **0** | **S2** — the compiler emits it for git/commit intents; the executor honestly reports "unavailable". Accepted gap, tracked |
| `cap://research/web` | **0** | **S2** — accepted gap; needs network policy + provider boundary |
| `cap://interaction/browser` | **0** | **S3** — accepted gap; belongs behind the trust pipeline |

The three zero-coverage capabilities are the entire tracked backlog: `ontology/known-gaps.json`.
**The roadmap is now `known-gaps.json` shrinking to empty** — not a wish list.

### D. Dormant inventory — built but unwired

Declared in `ontology/system.json → dormant` (10 entries) so the audit reports them as *known*
instead of silently dead. Each must be **wired or deleted** by its stated revisit trigger; dormancy
is a schedule, not a state:

`saf/models/kimi.py`, `saf/models/openrouter.py`, `saf/models/abacus.py`, `saf/models/base.py`
(provider mesh, no credentials), `saf/economy/scoring.py` (no caller), `saf/transport/grpc.py`,
`saf/transport/local.py` (no external import), `config/policy.yaml`, `config/providers.yaml`
(boilerplate; policy/providers are code today), plus test-only reachable helpers.

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
| F-2 | S3 | PyYAML is declared as a SAF dependency but never imported; the kernel is genuinely stdlib-only (`dependencies = []`) and SAF does use `pydantic` for its contracts | `grep -rn 'import yaml'` → 0 hits; `grep -rn 'import pydantic'` → `saf/core/contracts.py` | Drop PyYAML from `sovereign-agent-fabric-v20/pyproject.toml`, or keep only if a config loader lands |
| F-3 | S3 | Docker/compose describe kernel in `--demo` mode; SAF has no image, and the compose file has no healthcheck/restart policy | `Dockerfile`, `docker-compose.yml` | Non-demo entrypoint + `restart: unless-stopped` + healthcheck |

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
python3 tools/audit.py                            # 0 unexpected findings; 3 known (accepted) gaps (9 checks)
python3 tools/audit.py --json                     # same, machine-readable
python3 tools/audit.py --render-ontology          # regenerate docs/11-ontology.md
PYTHONPATH=src python3 -m unittest discover -s . -p 'test_*.py' -q    # 87 tests OK (includes test_audit, 12 audit tests)
cd sovereign-agent-fabric-v20 && PYTHONPATH=. python3 -m pytest -q     # 64 passed
pip install --break-system-packages coverage      # pip is PEP-668 managed in this sandbox
cd .. && PYTHONPATH=src python3 -m coverage run --source=src/effective_scale -m unittest discover -s . -p 'test_*.py' -q && python3 -m coverage report   # 81 %
cd sovereign-agent-fabric-v20 && PYTHONPATH=. python3 -m coverage run --source=saf -m pytest -q && python3 -m coverage report   # 76 %
gh pr list --state all; gh issue list --state all  # 2 PRs, 0 issues — the evidence for Part 1
```

## Part 5 — Attachments

The attached **PDF and image never reached the workspace**: no `*.pdf` or image file exists anywhere
under `/home/user` or `/tmp`, and `/tmp/arena-workspace/` contains only this session's own code
diff artifacts. They cannot be reviewed — and this audit will not pretend otherwise. **Re-upload
them and the review will be added as Part 6 of this document**, applied specifically to the gaps in
Part 2 (B-2 operability and C capability backlog are the most likely places external concepts
land). Until then, no conclusion from those attachments is claimed.

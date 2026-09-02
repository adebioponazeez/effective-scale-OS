# SAF V20 — Review vs. Architecture & Hardening Record

Review date: 2026-09-02 · Scope: `sovereign-agent-fabric-v20/` as retrieved from
the Drive source, checked against `01-final-system-architecture.md` (§1–§32),
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
| 4. observed | ✅ independent kernel observes the capability plan (workflow state, event nodes) |
| 5. independently verified | ✅ save-proof + hash-chained evidence ledger, tamper tests |
| 6. evidence recorded | ✅ ledger appends; chain verified in tests |
| 7. policy satisfied | ✅ policy gate before ranking/execution; tests |
| 8. rollback/recovery | ⚠️ quarantine/recovery proven for torn writes; full runtime rollback (snapshot→restore) is future work |

## 4. Honest boundaries (unchanged, by design)

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
pytest -q                      # 31 passed (includes live embedded-kernel test)

# integrate with a running effective-scale-OS:
PYTHONPATH=../src python3 -m effective_scale --store /tmp/es.db \
    --listen 127.0.0.1:8080 --admin-token dev &
saf run --es http://127.0.0.1:8080 --token "$TOKEN" "refactor repository and run tests"
```

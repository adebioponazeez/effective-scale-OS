# CAPABILITIES.md — what this system does, and what you can build with it today

This is the **build map**: every surface that exists, what composes with what, and the honest
boundary of what does *not* exist yet. It is not a wish list — the inventory section is verified by
`tools/audit.py` (`C-capmap`: every declared capability must appear here, and nothing here may
invent one), and every recipe below runs against the real binary.

Two halves, one system:

- **`effective-scale-OS` (the kernel)** — the durable *execution plane*: it decides where work runs,
  records every state transition, retries, times out, scales, and never loses a commit.
- **`sovereign-agent-fabric-v20` (SAF)** — the *decision plane*: it compiles an intent into
  capabilities, ranks resources, gates anything dangerous, executes, verifies, and keeps a
  tamper-evident receipt.

You can use either half alone. The leverage is in the seam: SAF decides *what*, the kernel
guarantees *that it actually happened, exactly once, and is observable*.

---

## 1. What you can build — twelve working recipes

Each row is a thing you can ship today, the surface that does it, and the proof that it works.

| # | Build this | Surface | Start with |
|---|---|---|---|
| 1 | **An agent that runs real work end-to-end** — intent → capability plan → scheduled node → worker executes → evidence | `saf run/execute/worker` + `/v1/workflows` + `/v1/attempts` (ADR-006 worker protocol) | `saf worker --es URL --token T --workspace .` |
| 2 | **A bounded-authority git bot** — commits a repo, refuses to run hostile messages, read-only unless you give it a message | `cap://software/git/operate` → `saf://git` | `saf execute "commit the changes in \"fix\": patch" --workspace repo` |
| 3 | **A fetch-and-prove research step** — pulls caller-named URLs, hashes the bytes, keeps a 600-char excerpt, refuses unreachable/private hosts by default | `cap://research/web` → `saf://web` + `NetworkPolicy` | set `SAF_NETWORK_ALLOW=docs.python.org`, then `saf execute "fetch https://docs.python.org/3/"` |
| 4 | **Any plan, run as a crash-safe DAG** — fan-out/fan-in, per-node retry with exponential backoff + jitter, per-node and whole-workflow timeouts, cancel, manual node retry, dead-letter | `/v1/workflows` + `POST /v1/workflows/{id}/retry-node/{n}` | `POST /v1/workflows {"nodes":[{"id":"a","exec":{"topic":"x"},"timeout":30,"retry":{"max":3}}]}` |
| 5 | **Recurring scheduled work** — cron templates cloned into independent runs with fresh state | `schedule: "*/5 * * * *"` on a workflow; `run_clone` | `POST /v1/workflows {"schedule":"0 3 * * *", …}` |
| 6 | **An event-driven pipeline** — partitioned (CRC32-keyed) topics, consumer groups with offsets, at-least-once delivery, backpressure instead of memory growth, dead-letter queue | `/v1/events`, `/v1/events/{topic}/ack`, `/v1/events/{topic}/dlq` | `POST /v1/events {"topic":"orders", "key":"o-1", "payload":{…}}` |
| 7 | **A worker fleet you can lose nodes from** — lease slots, claim/ heartbeat/ complete with fencing nonces, `deadline_exceeded` instead of silent overrun, expiry reclaim | `/v1/attempts/*`, `/v1/leases` | `POST /v1/attempts/{id}/claim {"worker_id":"w1"}` → heartbeat → complete |
| 8 | **Elastic capacity** — workloads with CPU *and* memory budgets (separately compared, `K23`), min/max replicas, metric-driven autoscaling, four distinct placement policies, cordon/drain | `/v1/workloads`, `/v1/nodes`, `POST /v1/workloads/{id}/scale` | `make run`, then `POST /v1/workloads {"policy":"bin_pack", …}` |
| 9 | **A multi-tenant control plane** — namespaces, scoped tokens (read/write/admin), rate limits, idempotency keys, trace ids, admin audit trail | `/v1/tokens`, `/v1/namespaces`, `/v1/audit`, `Idempotency-Key` header | `POST /v1/tokens {"namespace":"team-a","scopes":["read","write"]}` |
| 10 | **An offline-first agent** — work recorded in a durable outbox while the kernel is down, replayed idempotently (payload-hash key) when it returns, `sync` exits 1 while anything is deferred | `saf run --no-queue`, `saf sync` | `saf run --es http://unreachable "inspect repository"` then `saf sync` |
| 11 | **A system that can prove and undo its own changes** — hash-chained append-only evidence ledger, tamper detection, byte-level save-proof, snapshot/restore rollback points | `saf verify/ledger/rollback`, `cap://verification/save-proof` | `saf execute … && saf verify && saf rollback latest --dry-run` |
| 12 | **A capability runtime of your own** — the extension seam (see §3) | `Registry.register` + ontology + tests | `PYTHONPATH=sovereign-agent-fabric-v20 python3 -m saf.cli.main capabilities` |

**Verified end-to-end:** recipe 1 is proven live in `sovereign-agent-fabric-v20/tests/test_integration.py`
— a real embedded kernel schedules two lease-bound nodes, a real SAF worker claims and executes them
in dependency order, the workflow lands `succeeded`, and the evidence chain verifies afterwards.

---

## 2. The declared capability catalog

The compiler maps intent → capabilities; the registry decides which resource satisfies each one.
`C-caps` fails the build if this table and the running registry disagree.

| Capability | Runtime(s) | Deterministic | Notes |
|---|---|---|---|
| `cap://software/repository/inspect` | `saf://local` | yes | file listing, sizes, hashes |
| `cap://software/testing/execute` | `saf://local` | yes | runs a configured test command, records exit code |
| `cap://verification/save-proof` | `saf://local` | yes | byte-level proof that a mutation landed |
| `cap://software/code/refactor` | `agent://cursor-cli`, `agent://codex-cli`, `agent://opencode`, `agent://aider` | no | needs one CLI on `PATH` |
| `cap://software/code/inspect` | `agent://pi` | no | needs `pi` on `PATH` |
| `cap://software/agent/execute` | all five CLI adapters | no | full-agent delegation |
| `cap://general/agent/execute` | all five CLI adapters | no | fallback for unclassified intents |
| `cap://software/git/operate` | `saf://git` | yes | fixed argv, no shell, message capped + bounded |
| `cap://research/web` | `saf://web` | yes | deny-by-default network policy, SSRF guard |
| `cap://interaction/browser` | **none** | — | **declared intent only** — the one remaining `known-gaps.json` entry |

Nine of ten are satisfied. The browser runtime is deliberately blocked on the trust pipeline: a
browser that renders hostile content needs a sandbox and a content-trust boundary first.

---

## 3. The extension seam — how to add the eleventh capability

The system is built so that "add a capability" is a bounded, testable change, not a fork:

1. **Write the runtime** — a class with `resource_id`, `kind`, `capability_ids`, `trust`, and an
   async `execute(request) -> AgentResult`. Nothing in the fabric imports your module directly.
2. **Register it** — `Registry.register(resource_id, runtime)` in `saf/runtime/bootstrap.py`.
   The resolver ranks it deterministically (fit → trust → cost → environment); no config editing,
   no plugin discovery, no ordering magic.
3. **Declare it in the ontology** — `ontology/system.json` gains the capability with
   `satisfied_by: [your resource]`. The compiler pattern that emits the id goes in
   `saf/core/compiler.py`.
4. **List it here** — §2 of this file.
5. **Prove it** — tests for the runtime plus `tools/audit.py` (which will now fail if the registry,
   the ontology and this document disagree) and `tools/audit.py --render-ontology`.

That loop is the reason `cap://research/web` and `cap://software/git/operate` landed as *small*
slices: `netpolicy.py` (90 stmts) + `web.py` (97 stmts) + 28 tests, with the audit catching the
two stale dormant entries they left behind.

---

## 4. What the kernel gives you that is easy to miss

These are the properties that make the recipes above safe to run unattended — each is an invariant
in `ontology/system.json` with a test that enforces it:

| Property | Invariant | What it buys you |
|---|---|---|
| Single writer, snapshot reads | K1, K2 | no torn state, no lock contention in your handlers; every mutation is serialized |
| Table-checked state machines | K3 | an illegal transition raises instead of corrupting history |
| Fenced, idempotent completion | K4, K5, K10 | duplicate/late workers cannot double-apply work |
| Idempotent POSTs | K6 | retrying a client request is safe by construction |
| Namespace isolation | K7 | multi-tenancy that cannot leak across teams |
| Bounded inputs/results | K8 | one hostile client cannot exhaust memory |
| Event backpressure | K9 | producers are told to slow down instead of OOM-ing the kernel |
| Retry storms bounded | W4 (docs/03) | max attempts + DLQ + `workflow.deadletter` alert |
| Honest readiness | K21 | a kernel that cannot commit is removed from the load balancer |
| Restart safety | K22 | `make soak` proves state stays bounded and no attempt duplicates after a crash |
| Load shedding | K24 | saturated backlog ⇒ `503 + Retry-After`, never unbounded queues |
| Zero runtime dependencies | K20 (kernel) | copy the tree onto any machine with Python 3.11+; nothing to install |

---

## 5. How you deploy and operate it

| Environment | Artifact | What it declares |
|---|---|---|
| Linux | `deploy/systemd/effective-scale.service` | `Restart=always`, 35 s drain, hardening |
| Windows | `deploy/windows/install-service.ps1` | SCM restart ladder via `sc.exe failure` |
| Kubernetes | `deploy/k8s/*.yaml` | startup/readiness/liveness probes, 30 s termination grace |
| Docker | `Dockerfile` + `docker-compose.yml` | image `HEALTHCHECK` on `/v1/health/ready` |
| CI | `deploy/ci/ci.yml` | audit + both suites + soak (see the caveat below) |

`C-ops` fails the build if any artifact loses its restart policy or probe. Health is three-valued
and honest: `/v1/health/live` (process alive), `/v1/health/ready` (can commit — pings the store
through the writer thread), `/v1/status` (+ loops, leases, writer bulkhead/breaker).

---

## 6. The honest boundary — what you cannot build yet

Stated plainly, because a capability map that hides its edges is a liability:

| Limit | Consequence | Tracked |
|---|---|---|
| **No browser runtime** (`cap://interaction/browser`) | You cannot drive a real UI/session; web work is fetch-and-hash only | `known-gaps.json` (#1 in README §5) |
| **CI does not gate on GitHub** — the workflow file is untracked (token lacks `workflows` scope) | Green-locally is a habit, not a guarantee; the tracked copy sits at `deploy/ci/ci.yml` | README §5 (#2) |
| **No dashboard or alert rules** | Metrics and status exist but nothing *consumes* them; "is the fleet producing?" is a manual query | GAP-AUDIT B-5 |
| **No long-duration campaign** | The soak is minutes-short; slow leaks (fds, WAL growth) are untested | GAP-AUDIT B-2b |
| **Non-deterministic capabilities need external CLIs installed** | the code-refactor and code-inspect capabilities (`cap://software/code/refactor`, `cap://software/code/inspect`) and `cap://general/agent/execute` report `unavailable` with a per-resource reason when their CLI is missing — they fail loudly, never silently | `saf capabilities`, resolver output |
| **The Go port is a blueprint** | `docs/10-go-porting-blueprint.md` is a plan, not code | that document |
| **LLM access is optional and key-gated** | `OpenRouterAdapter` registers only when `OPENROUTER_API_KEY` exists; everything deterministic works without it | `build_providers()` |

None of these is hidden in prose: the first four have ids in `GAP-AUDIT.md`, and the audit refuses
to let a new one appear without being written down.

---

## 7. Verify this document

```bash
cd /home/user/effective-scale-OS
python3 tools/audit.py                     # C-capmap: this file ↔ ontology capabilities agree
PYTHONPATH=sovereign-agent-fabric-v20 python3 -c \
  "from saf.runtime.bootstrap import build_registry as b; print([r.resource_id for r in b().all()])"
make test && make test-saf && make soak    # the recipes above are covered by these suites
cd sovereign-agent-fabric-v20 && PYTHONPATH=. python3 -m pytest tests/test_integration.py -q
```

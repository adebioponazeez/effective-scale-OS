# 09 — API Reference (v1)

Base path `/v1`. JSON in/out. Auth: `Authorization: Bearer <token>` (API token) or
`X-Admin-Token: <bootstrap>` for admin routes. Errors: `{"ok":false,"error":{"code","message"}}`;
4xx codes are stable (`validation_error`, `unauthorized`, `forbidden`, `not_found`,
`conflict`, `rate_limited`, `capacity_exhausted`, `illegal_state_transition`,
`backpressure`); 5xx is always `internal_error`. On the worker routes: fenced/stale worker
token, dead lease, or double claim → `409 conflict`; unknown or foreign-namespace attempt →
`404 not_found`; oversized `result` → `400 validation_error`. `X-Trace-Id` echoes/accepts tracing.

## Health & meta
| Method | Path | Notes |
|---|---|---|
| GET | `/health/live` | no auth; process liveness |
| GET | `/health/ready` | no auth; store readiness (503 if down) |
| GET | `/status` | leader, counts, loop errors |
| GET | `/metrics` | registry snapshot |
| GET | `/me` | claims of current token |
| GET | `/audit?limit=100` | admin scope; append-only trail |

## Namespaces & tokens (admin)
| Method | Path | Body |
|---|---|---|
| POST | `/namespaces` | `{"name":"demo"}` |
| GET | `/namespaces` | list |
| POST | `/tokens` | `{"namespace":"demo","scopes":["read","write"],"ttl":86400}` → `{token, token_id, ...}` |

## Workloads
| Method | Path | Notes |
|---|---|---|
| POST | `/workloads` | `{name, image, replicas, cpu, memory, min_replicas?, max_replicas?, policy?, node_tags?, pin?, spread?, priority?, scaler?}`; supports `Idempotency-Key` |
| GET | `/workloads` | list (namespace-scoped) |
| GET | `/workloads/{wid}` | one |
| DELETE | `/workloads/{wid}` | remove |
| POST | `/workloads/{wid}/scale` | `{"replicas": N}` within min/max |
| POST | `/workloads/{wid}/metrics` | `{"cpu":0.8,"memory":512}` (scaler inputs) |

`scaler = {"cooldown_seconds":60, "metrics": {"cpu": {"target":0.65}, "queue": {"target":10}}}`.
Deadband 10 %, scale-down protection 180 s, bounded by min/max.

## Nodes (admin)
| Method | Path | Notes |
|---|---|---|
| POST | `/nodes` | `{name, cpu, memory, tags:["prod"]}` |
| GET | `/nodes` | list with used/free capacity |
| POST | `/nodes/{nid}/cordon` | `{"cordon": true|false}` |
| POST | `/nodes/{nid}/drain` | drains leases; no new placement |

## Workflows
| Method | Path | Notes |
|---|---|---|
| POST | `/workflows` | DAG; `Idempotency-Key` supported; validates cycles/dangling deps |
| GET | `/workflows`, `/workflows/{wid}` | state views |
| POST | `/workflows/{wid}/cancel` | revokes running attempts; idempotent completions ignored |
| POST | `/workflows/{wid}/retry-node/{nid}` | manual retry of terminal node |
| GET | `/attempts` | query: `state=running`, `claimable=true|false`, `workflow_id`, `lease_id`,
`worker_id`, `limit` — namespace-scoped worker inbox |
| GET | `/attempts/{aid}` | one attempt (404 for another namespace) |
| POST | `/attempts/{aid}/claim` | `{"worker_id","ttl_seconds"?}` → `{attempt, nonce, lease_id, deadline}` — binds a worker, rotates the fencing token |
| POST | `/attempts/{aid}/heartbeat` | `{"worker_id","nonce","ttl_seconds"?}` → `{lease_expires_at, deadline}` — renews the lease |
| POST | `/attempts/{aid}/complete` | `{"ok":true,"worker_id","nonce","result":{...}}` or `{"ok":false,"error":"..."}` — the executor protocol |
| GET | `/leases` | lease slots with holder, expiry and state (namespace-scoped) |

Node shape:
```json
{"id":"transform","depends_on":["extract"],"exec":{"workload_id":"…"},"timeout":60,
 "retry":{"max":3,"base_seconds":1,"max_seconds":60,"jitter":0.2},
 "max_concurrency":16}
```
A node execs exactly one of `exec.workload_id` (lease slot) or `exec.topic` (publish event).
Workflow: `{"name","nodes",...,"timeout":300,"schedule":"*/15 * * * *","dead_letter":true}`.
`scheduled` workflows are templates; each cron fire creates a fresh run.

### Executor protocol (ADR-006)

A worker never needs internal ids: it **discovers** claimable attempts, **claims** one (which
binds the attempt's lease slot to `worker_id` and returns a fencing `nonce`), **heartbeats** to
hold the lease, and **completes** with the nonce. Semantics:

- `GET /attempts` only returns attempts of the caller's namespace; another namespace's attempt
  is always `404` (ids are not capabilities).
- Completing a **claimed** attempt without its live nonce is `409 conflict` (fenced). Re-claiming
  as the same `worker_id` rotates the nonce, so a stalled copy of a worker is fenced out.
- Completing an already-terminal attempt is an idempotent `200` with `"idempotent": true`.
- A lease-bound attempt whose lease expired is failed by the kernel (`error: "lease_expired"`) and
  the node's retry policy applies; the expired slot is reaped and re-granted by the scheduler.
- `result` must be a JSON object ≤ 64 KiB (`config.attempt_result_max_bytes`) and is recorded on
  the attempt (visible in `GET /attempts/{aid}` and in `GET /workflows/{wid}` under `attempts`).
- Claim/heartbeat/completion are audited (`attempt.claim`, `attempt.complete`) with the worker id
  as actor.

```bash
# worker loop (sketch)
curl -s "$ES/v1/attempts?claimable=true&state=running&limit=1" -H "Authorization: Bearer $T"
curl -s -XPOST "$ES/v1/attempts/$AID/claim" -H "Authorization: Bearer $T" \
     -d '{"worker_id":"w1","ttl_seconds":60}'
curl -s -XPOST "$ES/v1/attempts/$AID/heartbeat" -H "Authorization: Bearer $T" \
     -d '{"worker_id":"w1","nonce":"…","ttl_seconds":60}'
curl -s -XPOST "$ES/v1/attempts/$AID/complete" -H "Authorization: Bearer $T" \
     -d '{"ok":true,"worker_id":"w1","nonce":"…","result":{"evidence_hash":"…"}}'
```

## Events
| Method | Path | Notes |
|---|---|---|
| POST | `/events` | `{topic,key,payload,schema_version}`; 429 on lag backpressure |
| GET | `/events/{topic}?group=g&limit=10` | next undelivered per partition (at-least-once) |
| POST | `/events/{topic}/ack` | `{group, event_id, ok, error}` |
| GET | `/events/{topic}/dlq` | poison messages (after `max_attempts`, default 5) |

## Idempotency
POSTs with `Idempotency-Key: <k>` are applied once; replay returns the original body plus
`"replayed": true`; same key + different payload → `409 conflict`.

## Limits
Body 1 MiB; rate 6000 req/min per client (configurable); event lag 1000 (configurable);
workflow concurrency default 16 per node.

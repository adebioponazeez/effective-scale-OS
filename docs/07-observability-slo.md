# 07 — Observability & SLOs

## 1. Logs (stdout, JSON lines)

Every record: `{ts, level, event, ...fields}`. Correlation: `X-Trace-Id` request header is
honored end-to-end (API → workflow → events; attempt/trace carried on records); when absent a
request id is generated per call.

Canonical events (alert-worthy in bold):

- `kernel.start/stop`, **`kernel.loop_crashed`**, **`kernel.watchdog.stall`**, **`api.unhandled`**
- `workflow.submitted/finalized`, **`workflow.deadletter`**, `workflow.cancel_requested`,
  `workflow.attempt_dispatched`
- `event.published/delivery_failed`, **`event.deadletter`**
- `lease.grant/release`, `workload.scale`, `leader.acquired`, **`leader.demoted`**

## 2. Metrics endpoint (`GET /v1/metrics`, JSON)

| Metric | Type | Meaning |
|---|---|---|
| `loop.<name>.last_ts` | gauge | last progress per loop; watchdog alarms on gap |
| `scheduler.leases.granted/released` | counter | placement activity |
| `scaler.decisions` / `scaler.desired.replicas` | counter/gauge | autoscale activity |
| `workflow.submitted/finished/failed/cancelled/deadletter` | counters | workflow plane health |
| `workflow.node_latency` | histogram | node execution latency buckets |
| `events.published/delivered/deadletter` | counters | bus throughput/poison rate |
| `eventbus.pending` | gauge | current lag (backpressure watch) |
| `metric.<wl>.<name>` | gauge | worker-reported metrics (scaler inputs) |
| `writer.pending` / `writer.queued` | gauge | client writes waiting on the single writer, and queued tasks |
| `writer.breaker_open` | gauge | 1 while the write path is shedding load (`503 overloaded`) |

`/v1/status` exposes leader, write count, per-plane resource counts, loop errors and the writer
isolation block (`breaker`, `pending`, `backlog_limit`, `queued`) — the "is the kernel healthy"
single pane. `writer.breaker = open` is the operator's signal that clients are being shed at 503.

## 3. Health

| Endpoint | Contract |
|---|---|
| `GET /v1/health/live` | process alive **and** event loop progressing — independent of store |
| `GET /v1/health/ready` | store open + schema loaded; 503 otherwise |
| `GET /v1/status` | detailed (leader, counts, loop errors) for ops dashboards |

## 4. Audit trail (`GET /v1/audit`, admin scope)

Append-only rows: `ts, actor, action, resource, outcome, detail, trace_id`.
Actions recorded: namespace/token create, workload create/scale/delete, node
create/cordon/drain, lease grant/release, event publish, workflow submit/cancel/retry,
cron fire, scaler decision. Covers the "who changed what and why" question for every
mutating path.

## 5. SLOs & alerting (v1 targets)

| SLO | Target | Alert when |
|---|---|---|
| API p95 latency (1 KB read, local) | < 25 ms | > 100 ms for 5 min |
| Availability (per process) | 99.9 % | watchdog exit > 2/24 h |
| Crash recovery | < 2 s cold | > 5 s |
| Duplicate dispatch of same attempt | 0 (invariant) | any — page |
| Event lag | < 1000 | lag ≥ 80 % of max_lag for 5 min |
| Workflow dead-letter rate | < 0.1 % | > 1 %/h |
| Autoscaler oscillation | ≤ 1 step/cooldown | flapping metric |
| Secret leak in logs/API | 0 | any — page |

## 6. Dashboards (3 panels minimum)

1. **Correctness**: deadletter counters, duplicate-attempt invariant, loop stalls, audit rate.
2. **Plane load**: per-plane counters + gauges, write count, queue lag.
3. **SLO**: latency histogram p95/p99, availability (uptime), recovery time.

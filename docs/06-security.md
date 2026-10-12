# 06 — Security Model

## 1. Threat model (in scope)

- Unauthenticated API abuse, token theft/replay, cross-tenant data access, secret leakage via
  logs/API, audit forgery, malformed payload attacks, resource exhaustion (body size, rate),
  supply-chain (zero runtime deps ⇒ no third-party supply chain by construction).
- **Server-side request forgery and egress abuse** (agent fabric): every outbound request from
  `saf://web` passes a deny-by-default `NetworkPolicy` — see §5a.

**Out of scope:** TLS termination (delegated to front proxy / k8s ingress), OS hardening,
mTLS service mesh (documented adapter), multi-factor auth (delegated to Identity provider;
the JWT hook is the adapter).

## 2. Tokens

- Format `es1.<payload_b64>.<hmac_hex>`; HMAC-SHA256 with a >=16-char configured secret
  (`ES_AUTH_SECRET`). Verify is `hmac.compare_digest` (constant time).
- Payload: `{v:1, ns, sc:[read|write|admin], iat, exp, jti}`. JWT hook is an adapter behind
  `TokenService` (same `Claims` type).
- At rest: only `sha256(raw)` is stored (plus jti/exp/scope) — a DB dump is **not** replayable.
- Scope model: `read` (reads), `write` (mutations), `admin` (nodes, tokens, namespace, audit).
  Default deny; missing scope → 403; expired/unverifiable → uniform 401.

## 3. Bootstrap

- `POST /v1/namespaces`, `/v1/tokens` and node/admin routes accept `X-Admin-Token` equal to the
  configured `ES_ADMIN_TOKEN`. In dev (no token configured) they are open — loudly documented;
  production MUST set both secrets. After bootstrapping, rotate to token-only in the proxy.

## 4. Data hygiene

- Namespace isolation enforced at two layers: permission check (403) and per-resource lookup.
- Secrets redacted by the logger (`secret`, `token`, `password`, `authorization`, etc.) and in
  API bodies via `redact()`.
- Audit log is append-only — no update/delete API exists for audit rows.

## 5. Abuse controls

| Control | Default | Behavior |
|---|---|---|
| Body size | 1 MiB | 413-style DomainError (400 with code) — never parsed fully |
| Rate limit | 6000 req/min/key | sliding window, `Retry-After` header, per client+version |
| Event backpressure | max_lag 1000 | publisher gets 429; no unbounded growth |
| Workflow concurrency | 16/node | bounded fan-out |
| Lease ambiguity | renewal refused after TTL/2 | bounded double-run window |

## 5a. Outbound network (agent fabric)

The kernel itself makes no outbound calls. The SAF subproject can, so it does so through one
policy object (`saf/core/netpolicy.py`) that is constructed once and never mutated per request.

| Control | Default | Why |
|---|---|---|
| Host allowlist | **empty — deny by default** | an unconfigured deployment cannot exfiltrate or be used as a pivot; `SAF_NETWORK_ALLOW=host1,*.host2` opts in |
| Scheme allowlist | `https` only | `http` must be requested explicitly |
| Port allowlist | `443`, `80` (`SAF_NETWORK_PORTS` extends it) | no scanning of arbitrary services |
| SSRF address guard | on | the host is resolved and **every** address is checked: private, loopback, link-local, reserved, multicast and unspecified are refused (this is what blocks `169.254.169.254`, the cloud metadata service); `allow_private` is an explicit opt-in for on-prem |
| Redirects | ≤ 3, re-validated per hop | a permitted host cannot bounce a fetch to a forbidden one |
| Response bound | `max_bytes` (2 MB default) | oversized bodies are truncated and flagged, never buffered |
| Time bound | `timeout_s` (15 s default) | a slow endpoint cannot pin a worker |

The runtime is fetch-and-hash, not a crawler: it contacts **caller-supplied URLs** and returns
status, content type, byte count, sha256 and a bounded excerpt as evidence. It writes nothing to
disk and has no search capability (documented in `saf/agents/web.py`). Every decision is testable
offline — `saf/tests/test_web_runtime.py` asserts the deny paths (blank allowlist, bad scheme,
wrong port, loopback, metadata IP, cross-policy redirect) without touching the internet.

## 6. Deployment checklist (production)

1. `ES_AUTH_SECRET` >= 32 random bytes; `ES_ADMIN_TOKEN` >= 32 random bytes; both via secret refs.
2. Terminate TLS at ingress; restrict `/v1/tokens` (admin) to egress allowlist.
3. Store volume: PVC with `fsync`-capable storage (WAL + FULL needs it).
4. Enable JSON logs → collector; alert on `kernel.watchdog.stall`, `api.unhandled`, 429 storms,
   `workflow.deadletter`, `events.deadletter`.
5. Restrict network so only the control-plane port is open; workers call back over mTLS if
   external (documented adapter).
6. Agent fabric: set `SAF_NETWORK_ALLOW` to the exact research hosts you permit (never `*`), keep
   `allow_private` off, and leave provider keys (`OPENROUTER_API_KEY`) unset until a provider is
   needed — the adapter registers itself only when the key exists.
